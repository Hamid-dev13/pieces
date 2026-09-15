"""Telegram : whitelist, réception des documents, réponses."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Chat, Message, TelegramObject, Update, User

from . import classify, db, extract, storage
from .config import MAX_FILE_BYTES, Config

logger = logging.getLogger(__name__)

router = Router()


def _event(update: Update) -> Any | None:
    """L'objet concret porté par l'update, quel que soit son type.

    On ne cite aucun type d'update par son nom : un `Update` n'en porte qu'un
    seul, et le lire ainsi fait que les types ajoutés par Telegram demain
    passent par le même contrôle au lieu de le contourner.
    """
    for name in type(update).model_fields:
        if name == "update_id":
            continue
        value = getattr(update, name, None)
        if value is not None:
            return value
    return None


def _sender(event: Any) -> User | None:
    return getattr(event, "from_user", None)


def _chat(event: Any) -> Chat | None:
    chat = getattr(event, "chat", None)
    if chat is not None:
        return chat
    # Un callback_query ne porte pas de chat : il porte le message cliqué.
    return getattr(getattr(event, "message", None), "chat", None)


class OwnerOnly(BaseMiddleware):
    """Ne laisse passer que le propriétaire, et seulement en privé.

    Posé en `outer_middleware` sur le dispatcher, donc traversé par tous les
    types d'updates — y compris les `callback_query` que la correction du
    classement et le choix entre candidats amèneront. Un garde recopié dans
    chaque handler y serait oublié.
    """

    def __init__(self, allowed_ids: frozenset[int]) -> None:
        self.allowed_ids = allowed_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update: Update = event  # type: ignore[assignment]
        carried = _event(update)
        sender = _sender(carried)
        chat = _chat(carried)

        # Un refus reste silencieux côté chat : répondre « non autorisé »
        # confirmerait l'existence du bot à qui le sonde. Cette ligne de log
        # est la seule trace, et c'est elle qui rend le refus vérifiable.
        if sender is None or sender.id not in self.allowed_ids:
            logger.warning(
                "Refusé : update %s de type %s, expéditeur %s",
                update.update_id,
                type(carried).__name__,
                sender.id if sender is not None else "inconnu",
            )
            return None

        # `chat_id` et `user_id` ne coïncident qu'en conversation privée. Dans
        # un groupe, un compte autorisé demanderait son permis devant des tiers.
        if chat is not None and chat.type != "private":
            logger.warning(
                "Refusé : update %s dans le chat %s de type %s",
                update.update_id,
                chat.id,
                chat.type,
            )
            return None

        return await handler(event, data)


def _store(payload: bytes, filename: str, config: Config) -> tuple[bool, str, str]:
    """Range le document. Renvoie (déjà connu, empreinte, chemin relatif).

    Le fichier est écrit **avant** l'insertion en base : si le conteneur meurt
    entre les deux, un fichier sans ligne est inoffensif — le prochain envoi du
    même document complétera la base. L'inverse laisserait une entrée pointant
    vers un fichier absent.
    """
    sha = storage.digest(payload)
    connu = db.find_by_sha(config.db_path, sha)
    if connu is not None:
        return True, sha, connu["chemin"]

    chemin = storage.save(payload, sha, config.documents_dir)
    try:
        db.insert_document(
            config.db_path,
            sha256=sha,
            fichier=filename,
            chemin=chemin,
            titre=Path(filename).stem,
        )
    except sqlite3.IntegrityError:
        # Deux envois simultanés du même document : la contrainte UNIQUE a
        # tranché, et le fichier écrit est le même octet pour octet.
        return True, sha, chemin
    return False, sha, chemin


def extract_into_db(config: Config, sha256: str, chemin: str) -> tuple[str, str]:
    """Extrait le texte d'un document déjà rangé et le complète en base.

    Renvoie (texte, source), où source vaut 'natif', ou 'aucun' quand le PDF
    n'a pas de couche texte — ce dernier cas part à l'OCR. Le texte est rendu
    en plus d'être écrit : le classement l'enchaîne, et le relire en base pour
    l'avoir serait un aller-retour pour rien.
    """
    texte, source = extract.from_pdf(config.documents_dir / chemin)
    db.update_text(config.db_path, sha256=sha256, texte=texte, source_texte=source)
    return texte, source


def ocr_into_db(config: Config, sha256: str, chemin: str) -> tuple[str, str]:
    """Passe un document à l'OCR et complète la base. Renvoie (texte, source).

    La tentative est notée dès lors que la réponse est définitive : un scan que
    l'OCR ne sait pas lire ne doit pas être repris à chaque redémarrage. Une
    panne passagère, elle, laisse le document en attente.
    """
    texte, source = extract.with_ocr(config.documents_dir / chemin, config.mistral_api_key)
    if source == "ocr":
        db.update_text(config.db_path, sha256=sha256, texte=texte, source_texte=source)
    if source != extract.UNAVAILABLE:
        # Un OCR injoignable ou un quota atteint doit pouvoir être repris :
        # seul un document vraiment illisible clôt la question.
        db.mark_ocr_attempted(config.db_path, sha256)
    return texte, source


def classify_into_db(config: Config, sha256: str, texte: str) -> tuple[
    classify.Classification | None, str
]:
    """Fait classer un document et complète la base. Renvoie (classement, état).

    L'état vaut 'llm' quand le document est classé, 'indisponible' quand Ollama
    ne répond pas — le document reste alors à `classe_par = 'aucun'` et
    repassera au prochain démarrage — et 'aucun' quand la réponse est
    inexploitable.
    """
    resultat, etat = classify.classify(
        texte, url=config.ollama_url, model=config.ollama_model
    )
    if resultat is None:
        return None, etat

    db.update_classification(
        config.db_path,
        sha256=sha256,
        type=resultat.type,
        titre=resultat.titre,
        emetteur=resultat.emetteur,
        date_document=resultat.date_document,
        date_expiration=resultat.date_expiration,
    )
    return resultat, etat


def _date_lisible(iso: str | None) -> str | None:
    """12/03/2024 plutôt que 2024-03-12. Une date partielle passe telle quelle."""
    if iso is None:
        return None
    morceaux = iso.split("-")
    if len(morceaux) == 3:
        annee, mois, jour = morceaux
        return f"{jour}/{mois}/{annee}"
    return iso


def resume(resultat: classify.Classification, fichier: str) -> str:
    """Ce que le bot annonce après avoir classé un document.

    Le classement est dit à voix haute, et pas seulement écrit en base : c'est
    ce qui rendra la correction de `cls-2` possible — on ne corrige que ce
    qu'on voit.
    """
    lignes = [f"Rangé — {fichier}."]

    libelle = classify.LIBELLES.get(resultat.type, resultat.type)
    titre = resultat.titre or fichier
    lignes.append(f"{libelle} · {titre}")

    if resultat.emetteur:
        lignes.append(f"Émis par {resultat.emetteur}")

    date_document = _date_lisible(resultat.date_document)
    if date_document:
        lignes.append(f"Daté du {date_document}")

    expiration = _date_lisible(resultat.date_expiration)
    if expiration:
        lignes.append(f"Expire le {expiration}")

    return "\n".join(lignes)


def _catch_up_extraction(config: Config) -> int:
    """Rattrape les documents rangés avant que l'extraction n'existe."""
    en_attente = db.awaiting_extraction(config.db_path)
    if not en_attente:
        return 0

    logger.info("Rattrapage : %d document(s) sans texte.", len(en_attente))
    traites = 0
    for document in en_attente:
        sha, chemin, fichier = document["sha256"], document["chemin"], document["fichier"]
        _, source = extract_into_db(config, sha, chemin)
        if source == "aucun":
            logger.info("Rattrapage par OCR : %s", fichier)
            _, source = ocr_into_db(config, sha, chemin)
        if source in ("aucun", extract.UNAVAILABLE):
            logger.warning("Rien à lire dans %s (%s).", fichier, source)
        else:
            traites += 1
            logger.info("Rattrapé (%s) : %s", source, fichier)
    return traites


def _catch_up_classification(config: Config) -> int:
    """Classe les documents lisibles que le modèle n'a pas encore vus.

    Il y en a de deux sortes, et c'est le même traitement : ceux qui sont
    arrivés avant cette story, et ceux qu'Ollama n'a pas su classer parce
    qu'il était éteint.

    Un Ollama toujours injoignable arrête la passe au premier document. Sans
    ça, un serveur sans modèle attendrait le délai d'expiration pour chacun de
    ses documents, à chaque démarrage — des minutes de démarrage pour un échec
    connu dès le premier appel.
    """
    en_attente = db.awaiting_classification(config.db_path)
    if not en_attente:
        return 0

    logger.info("Classement : %d document(s) en attente.", len(en_attente))
    classes = 0
    for document in en_attente:
        fichier = document["fichier"]
        resultat, etat = classify_into_db(
            config, document["sha256"], document["texte"]
        )
        if etat == classify.UNAVAILABLE:
            logger.warning(
                "Classement interrompu : Ollama ne répond pas. "
                "%d document(s) restent en attente.",
                len(en_attente) - classes,
            )
            break
        if resultat is None:
            logger.warning("Pas classé : %s", fichier)
            continue
        classes += 1
        logger.info("Classé %s : %s (%s)", resultat.type, fichier, resultat.titre)
    return classes


def catch_up(config: Config) -> int:
    """Reprend ce qui manque aux documents déjà rangés.

    Sans ce passage, un document arrivé pendant une panne — ou avant la story
    qui sait le traiter — resterait muet pour toujours : le renvoyer ne
    servirait à rien, le dédoublonnage l'écarterait avant d'y toucher.

    L'extraction d'abord, le classement ensuite : un document dont le texte
    vient d'être rattrapé est classé dans la foulée, sans attendre un second
    démarrage.
    """
    traites = _catch_up_extraction(config)
    _catch_up_classification(config)
    return traites


@router.message(F.document)
async def receive_document(message: Message, config: Config) -> None:
    document = message.document
    filename = document.file_name or "sans-nom.pdf"

    if not filename.lower().endswith(".pdf") and document.mime_type != "application/pdf":
        await message.reply("Je ne range que des PDF.")
        return

    # La taille annoncée évite un téléchargement qui échouerait de toute façon.
    if document.file_size and document.file_size > MAX_FILE_BYTES:
        await message.reply(
            f"Trop lourd : {document.file_size / 1024 / 1024:.1f} Mo. "
            "L'API Telegram plafonne l'envoi aux bots à 20 Mo."
        )
        return

    try:
        buffer = await message.bot.download(document)
    except TelegramBadRequest as erreur:
        # `file_size` n'est pas toujours renseigné : c'est ici que le plafond
        # des 20 Mo se rappelle vraiment. Sans ce garde-fou, le bot resterait
        # muet devant un document trop lourd.
        logger.warning("Téléchargement refusé pour %s : %s", filename, erreur)
        await message.reply(
            "Telegram refuse de me donner ce fichier. "
            "Au-delà de 20 Mo, l'API ne le sert pas aux bots."
        )
        return

    if buffer is None:
        logger.warning("Téléchargement vide pour %s", filename)
        await message.reply("Je n'ai rien reçu. Réessaie ?")
        return

    payload = buffer.read()

    if not storage.looks_like_pdf(payload):
        await message.reply("Ce fichier porte l'extension .pdf mais n'en est pas un.")
        return

    # Empreinte et écriture sont bloquantes : hors de la boucle asyncio.
    already_known, sha, chemin = await asyncio.to_thread(_store, payload, filename, config)

    if already_known:
        await message.reply(f"Déjà rangé — {filename} est en double.")
        logger.info("Doublon ignoré : %s (%s)", filename, sha[:12])
        return

    logger.info("Document rangé : %s (%s)", filename, sha[:12])
    texte, source = await asyncio.to_thread(extract_into_db, config, sha, chemin)

    if source == "aucun":
        # L'OCR se compte en minutes : sans ce mot, le bot paraîtrait planté.
        await message.reply(
            f"Rangé — {filename}.\n"
            "Aucun texte dedans : c'est un scan, je le déchiffre. Ça prend un moment."
        )
        texte, source = await asyncio.to_thread(ocr_into_db, config, sha, chemin)

        if source == extract.UNAVAILABLE:
            await message.reply(
                "L'OCR ne répond pas pour l'instant. Le document est rangé,\n"
                "je le relirai au prochain démarrage."
            )
            logger.warning("OCR indisponible : %s (%s)", filename, sha[:12])
            return
        if source != "ocr":
            await message.reply(
                "Je n'ai rien pu en tirer, même par OCR. Le document reste rangé.\n"
                "Une photo plus nette ou mieux cadrée marcherait peut-être mieux."
            )
            logger.warning("OCR sans résultat : %s (%s)", filename, sha[:12])
            return
        logger.info("Document lu par OCR : %s (%s)", filename, sha[:12])

    resultat, etat = await asyncio.to_thread(classify_into_db, config, sha, texte)

    if resultat is not None:
        await message.reply(resume(resultat, filename))
        logger.info("Classé %s : %s (%s)", resultat.type, filename, sha[:12])
        return

    # Le document est rangé et lisible ; seul le classement manque, et il
    # repassera au prochain démarrage. Le dire évite de renvoyer le document
    # en croyant qu'il n'est pas arrivé — le dédoublonnage l'écarterait.
    if etat == classify.UNAVAILABLE:
        await message.reply(
            f"Rangé — {filename}.\n"
            "Texte lu, mais le classement ne répond pas.\n"
            "Je le classerai au prochain démarrage."
        )
        logger.warning("Classement indisponible : %s (%s)", filename, sha[:12])
    else:
        await message.reply(
            f"Rangé — {filename}.\n"
            "Texte lu, mais je n'ai pas su quoi en faire. Il reste à classer."
        )
        logger.warning("Classement sans résultat : %s (%s)", filename, sha[:12])


@router.message()
async def anything_else(message: Message) -> None:
    await message.reply("Envoie-moi un PDF et je le range.")
