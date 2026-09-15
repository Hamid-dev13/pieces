"""Classement d'un document par un LLM local, via Ollama.

Le modèle ne rédige pas de prose : il remplit des cases. La sortie est
contrainte par un schéma JSON côté Ollama, si bien qu'un type hors de la liste
fermée est impossible par construction — pas « improbable », impossible.

Ce qui reste à vérifier ici, c'est ce que le schéma ne peut pas garantir : que
les dates ressemblent à des dates, et qu'un champ inventé ne se glisse pas dans
la base.

**L'appel est local.** Contrairement à l'OCR, rien ne sort du serveur : c'est
la raison d'être d'Ollama dans cette architecture, alors qu'une API distante
aurait classé mieux.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

# La liste fermée de CONCEPTION.md, découpée par objet plutôt que par nature
# juridique : une assurance auto se cherche avec la voiture, une assurance
# habitation avec le logement.
TYPES: tuple[str, ...] = (
    "identite",
    "permis_conduire",
    "vehicule",
    "impots",
    "banque",
    "sante",
    "logement",
    "emploi",
    "diplome",
    "transport",
    "facture",
    "autre",
)

# Ce que chaque type couvre, dit au modèle. Les frontières valent mieux que les
# étiquettes : « vehicule » sans exemples attrape mal une assurance auto.
DESCRIPTIONS: dict[str, str] = {
    "identite": "carte nationale d'identité, passeport, titre de séjour",
    "permis_conduire": "permis de conduire, attestation de points",
    "vehicule": "carte grise, assurance auto, contrôle technique, facture de garage",
    "impots": "avis d'imposition, déclaration de revenus, taxe foncière ou d'habitation",
    "banque": "RIB, relevé de compte, attestation bancaire, prêt",
    "sante": "carte vitale, mutuelle, ordonnance, résultats d'analyses, arrêt de travail",
    "logement": "bail, quittance de loyer, assurance habitation, état des lieux",
    "emploi": "CV, contrat de travail, fiche de paie, attestation employeur",
    "diplome": "diplôme, relevé de notes, certification, attestation de réussite",
    "transport": "billet d'avion, de train, réservation, carte de transport",
    "facture": "facture d'achat, abonnement, service, énergie, téléphonie",
    "autre": "rien de ce qui précède",
}

# Les mêmes types, tels qu'ils s'écrivent dans une phrase. La base garde
# l'identifiant technique ; c'est lui qui sert de filtre à `rec-1`, et il n'a
# aucune raison d'apparaître dans un chat.
LIBELLES: dict[str, str] = {
    "identite": "Pièce d'identité",
    "permis_conduire": "Permis de conduire",
    "vehicule": "Véhicule",
    "impots": "Impôts",
    "banque": "Banque",
    "sante": "Santé",
    "logement": "Logement",
    "emploi": "Emploi",
    "diplome": "Diplôme",
    "transport": "Transport",
    "facture": "Facture",
    "autre": "Non classé",
}

# Le type se décide dans l'en-tête. Envoyer quarante pages saturerait la
# fenêtre de contexte d'un 4B sans rien apporter.
MAX_TEXT_CHARS = 6000

# Le premier appel charge le modèle en VRAM : compter en dizaines de secondes,
# pas en secondes.
TIMEOUT_SECONDS = 180

# Même distinction qu'à l'OCR : Ollama éteint n'est pas un document
# inclassable. Le premier se réessaie au prochain démarrage, le second non.
UNAVAILABLE = "indisponible"

# Ollama contraint la sortie à ce schéma. Les champs absents reviennent en
# chaîne vide plutôt qu'en `null` : une union `["string", "null"]` se traduit
# mal en grammaire, alors qu'une chaîne vide est sans ambiguïté et se convertit
# ici même.
SCHEMA: dict = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(TYPES)},
        "titre": {"type": "string"},
        "emetteur": {"type": "string"},
        "date_document": {"type": "string"},
        "date_expiration": {"type": "string"},
    },
    "required": ["type", "titre", "emetteur", "date_document", "date_expiration"],
}

# Ce prompt est le résultat d'une mesure, pas d'une intuition. Une première
# version, plus courte, rendait « autre » pour un CV et inventait une date
# d'expiration sur un document qui n'en portait pas — voir `CONCEPTION.md`.
SYSTEM_PROMPT = """Tu classes des documents administratifs français.

Tu remplis un formulaire à partir du texte fourni. Règle absolue : tu ne
recopies que ce qui est écrit dans le document. Tu n'inventes jamais une valeur
pour remplir une case.

Les types possibles :
{types}

Choisis le type le plus précis qui convient. « autre » est un dernier recours :
ne l'emploie que si aucun des onze autres ne s'applique. Un CV est un document
d'emploi. Une attestation se classe d'après son objet, pas d'après le mot
« attestation ».

Les champs :
- type : exactement un type de la liste.
- titre : une ligne en français qui dit ce qu'est ce document, sans le nom du
  fichier ni de numéro. Par exemple « Avis d'imposition 2024 » ou
  « Carte grise Peugeot 208 ».
- emetteur : l'organisme ou l'entreprise qui a produit le document
  (« Direction générale des finances publiques », « EDF », « CPAM »). Vide si
  le document ne le nomme pas.
- date_document : la date que porte le document, au format AAAA-MM-JJ. Écris
  l'année seule si c'est tout ce qui est lisible. Vide si le document ne porte
  aucune date.
- date_expiration : à remplir uniquement si le document dit lui-même jusqu'à
  quand il vaut — « valable jusqu'au », « expire le », « date de fin de
  validité ». La plupart des documents n'expirent pas : un CV, une facture, un
  avis d'imposition, un relevé, un justificatif de paiement n'ont pas de date
  d'expiration. Dans le doute, laisse vide.

Ne remplis une date que si tu peux la retrouver dans le texte. Un champ vide
est un bon résultat quand l'information n'y est pas."""


@dataclass(frozen=True)
class Classification:
    """Ce que le modèle a lu dans un document."""

    type: str
    titre: str
    emetteur: str | None
    date_document: str | None
    date_expiration: str | None


def _prompt_systeme() -> str:
    lignes = "\n".join(f"- {nom} : {quoi}" for nom, quoi in DESCRIPTIONS.items())
    return SYSTEM_PROMPT.format(types=lignes)


# AAAA-MM-JJ, AAAA-MM, AAAA — les trois formes ISO 8601 utiles ici.
_ISO = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")
# Ce que le modèle rend quand il recopie le document plutôt que la consigne.
_FRANCAISE = re.compile(r"^(\d{2})[/.](\d{2})[/.](\d{4})$")


def _iso_date(brut: str, *, complete: bool = False) -> str | None:
    """Normalise une date en ISO 8601, ou renvoie None si ça n'en est pas une.

    Le schéma JSON contraint la forme de la réponse, pas son contenu : le
    modèle peut très bien rendre « 12/03/2024 », « mars 2024 » ou un numéro de
    dossier. Une date invalide vaut mieux absente qu'approximative — `exp-1`
    enverra des rappels à partir de cette colonne.

    `complete` exige le jour : une date d'expiration se compare à aujourd'hui,
    ce qu'une année seule ne permet pas.
    """
    brut = brut.strip()
    if not brut:
        return None

    francaise = _FRANCAISE.match(brut)
    if francaise is not None:
        jour, mois, annee = francaise.groups()
        brut = f"{annee}-{mois}-{jour}"

    trouve = _ISO.match(brut)
    if trouve is None:
        return None

    annee, mois, jour = trouve.groups()
    if complete and (mois is None or jour is None):
        return None

    # Une date doit exister : le 2024-02-31 d'un modèle qui hallucine n'a rien
    # à faire en base.
    try:
        date(int(annee), int(mois or 1), int(jour or 1))
    except ValueError:
        return None

    # Une date hors de toute plausibilité trahit un numéro lu comme une date.
    if not 1900 <= int(annee) <= date.today().year + 50:
        return None

    return brut


def _lire(charge: dict) -> Classification:
    """Convertit la réponse du modèle en valeurs bonnes pour la base."""
    type_lu = charge.get("type", "")
    if type_lu not in TYPES:
        # Le schéma rend ce cas impossible ; le vérifier coûte une ligne et
        # protège le jour où l'on passera par un autre moteur.
        logger.warning("Type hors liste rendu par le modèle : %r", type_lu)
        type_lu = "autre"

    titre = " ".join(str(charge.get("titre", "")).split())[:200]
    emetteur = " ".join(str(charge.get("emetteur", "")).split())[:200] or None

    return Classification(
        type=type_lu,
        # Peut être vide : c'est alors à l'appelant de garder le titre déjà en
        # base — le nom du fichier — plutôt que d'effacer le seul repère
        # lisible qu'ait la ligne.
        titre=titre,
        emetteur=emetteur,
        date_document=_iso_date(str(charge.get("date_document", ""))),
        date_expiration=_iso_date(str(charge.get("date_expiration", "")), complete=True),
    )


def classify(texte: str, *, url: str, model: str) -> tuple[Classification | None, str]:
    """Fait classer un texte par le modèle local.

    Renvoie `(classification, 'llm')` en cas de succès, `(None, 'aucun')` quand
    il n'y a rien à classer ou que la réponse est inexploitable, et
    `(None, 'indisponible')` quand Ollama ne répond pas — ce dernier cas doit
    être repris plus tard, pas classé sans suite.
    """
    extrait = texte.strip()[:MAX_TEXT_CHARS]
    if not extrait:
        return None, "aucun"

    corps = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _prompt_systeme()},
            {"role": "user", "content": extrait},
        ],
        "format": SCHEMA,
        "stream": False,
        "options": {
            # Un classement doit être reproductible : deux envois du même
            # document ne peuvent pas donner deux types.
            "temperature": 0,
            # Ollama tronque à 4096 par défaut, en silence — l'en-tête du
            # document passerait à la trappe sans que rien ne le signale.
            "num_ctx": 8192,
        },
    }).encode("utf-8")

    requete = urllib.request.Request(
        f"{url.rstrip('/')}/api/chat",
        data=corps,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(requete, timeout=TIMEOUT_SECONDS) as reponse:
            charge = json.load(reponse)
    except urllib.error.HTTPError as erreur:
        detail = erreur.read().decode("utf-8", errors="replace")[:300]
        if erreur.code == 404:
            # Ollama répond, mais le modèle n'a jamais été tiré. C'est une
            # erreur d'exploitation, pas un document illisible : le dire
            # franchement évite de chercher le défaut dans le prompt.
            logger.error(
                "Modèle %r inconnu d'Ollama. Lancer « ollama pull %s ». (%s)",
                model, model, detail,
            )
            return None, UNAVAILABLE
        logger.warning("Ollama a refusé la requête (%s) : %s", erreur.code, detail)
        return None, UNAVAILABLE
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as erreur:
        logger.warning("Ollama injoignable sur %s : %s", url, erreur)
        return None, UNAVAILABLE

    brut = (charge.get("message") or {}).get("content", "")
    try:
        champs = json.loads(brut)
    except json.JSONDecodeError:
        # Sous contrainte de schéma, ça ne devrait pas arriver. Si ça arrive,
        # c'est le modèle qu'il faut changer, pas le document : la ligne de log
        # porte de quoi le voir.
        logger.warning("Réponse illisible du modèle : %r", brut[:200])
        return None, "aucun"

    if not isinstance(champs, dict):
        logger.warning("Réponse inattendue du modèle : %r", brut[:200])
        return None, "aucun"

    return _lire(champs), "llm"
