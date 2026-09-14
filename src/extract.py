"""Extraction du texte des PDF.

`pdftotext` d'abord, en local. Quand il rend trop peu de texte, le document est
un scan et part à l'OCR de Mistral. `source_texte` garde la trace du chemin
emprunté — utile quand un document est mal classé, pour savoir si c'est
l'extraction ou le modèle qui a fauté.

**L'OCR envoie le document hors du serveur.** C'est le seul appel externe du
projet, et il porte le contenu intégral des documents. Voir `README.md`.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# En dessous de ce nombre de caractères visibles, le PDF n'a pas de couche
# texte exploitable. Le seuil est volontairement grossier : il sera réglé à
# `ext-2`, avec de vrais scans sous la main plutôt qu'au jugé.
USEFUL_TEXT_THRESHOLD = 100

# Un PDF malformé peut occuper pdftotext indéfiniment.
EXTRACTION_TIMEOUT_SECONDS = 60


def _visible_length(text: str) -> int:
    return len("".join(text.split()))


def from_pdf(path: Path) -> tuple[str, str]:
    """Renvoie (texte, source) où source vaut 'natif' ou 'aucun'.

    'aucun' signifie « rien d'exploitable ici » — pas une erreur, mais le
    signal que ce document attend l'OCR.
    """
    try:
        completed = subprocess.run(
            # -layout garde les colonnes et les tableaux lisibles : un avis
            # d'imposition perd son sens quand ses colonnes fusionnent.
            ["pdftotext", "-layout", "-enc", "UTF-8", "-q", str(path), "-"],
            capture_output=True,
            timeout=EXTRACTION_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        logger.error("pdftotext est absent de l'image.")
        return "", "aucun"
    except subprocess.TimeoutExpired:
        logger.warning("pdftotext a dépassé %s s sur %s", EXTRACTION_TIMEOUT_SECONDS, path.name)
        return "", "aucun"

    text = completed.stdout.decode("utf-8", errors="replace").strip()

    # pdftotext rend souvent un texte utilisable malgré un code non nul.
    if completed.returncode != 0 and not text:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        logger.warning("pdftotext a échoué sur %s : %s", path.name, detail or "sans détail")
        return "", "aucun"

    if _visible_length(text) < USEFUL_TEXT_THRESHOLD:
        logger.info(
            "Trop peu de texte dans %s (%d caractères) : probablement un scan.",
            path.name,
            _visible_length(text),
        )
        return text, "aucun"

    return text, "natif"


# --- OCR distant -----------------------------------------------------------

OCR_ENDPOINT = "https://api.mistral.ai/v1/ocr"
OCR_MODEL = "mistral-ocr-latest"
OCR_TIMEOUT_SECONDS = 180

# Une panne réseau ou un quota atteint n'est pas un document illisible : on
# doit pouvoir réessayer plus tard, là où un document refusé ne vaut pas
# qu'on y revienne.
UNAVAILABLE = "indisponible"


def with_ocr(path: Path, api_key: str) -> tuple[str, str]:
    """Fait lire le document par l'OCR de Mistral.

    Renvoie (texte, source) où source vaut 'ocr' si quelque chose a été lu,
    'aucun' si le document est définitivement illisible, et 'indisponible' si
    l'appel a échoué pour une raison passagère — auquel cas il faudra
    réessayer, pas classer l'affaire.
    """
    try:
        encode = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as erreur:
        logger.warning("Lecture impossible de %s : %s", path.name, erreur)
        return "", "aucun"

    corps = json.dumps({
        "model": OCR_MODEL,
        "document": {
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{encode}",
        },
        # Les images extraites ne servent à rien ici : on ne garde que du
        # texte, et les rapatrier alourdirait la réponse pour rien.
        "include_image_base64": False,
    }).encode("utf-8")

    requete = urllib.request.Request(
        OCR_ENDPOINT,
        data=corps,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(requete, timeout=OCR_TIMEOUT_SECONDS) as reponse:
            charge = json.load(reponse)
    except urllib.error.HTTPError as erreur:
        detail = erreur.read().decode("utf-8", errors="replace")[:300]
        if erreur.code in (401, 403):
            logger.error("MISTRAL_API_KEY refusée (%s) : %s", erreur.code, detail)
            return "", UNAVAILABLE
        if erreur.code == 429 or erreur.code >= 500:
            logger.warning("OCR indisponible (%s) : %s", erreur.code, detail)
            return "", UNAVAILABLE
        # 400 et consorts : le document lui-même ne passe pas.
        logger.warning("OCR a refusé %s (%s) : %s", path.name, erreur.code, detail)
        return "", "aucun"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as erreur:
        logger.warning("OCR injoignable pour %s : %s", path.name, erreur)
        return "", UNAVAILABLE

    pages = charge.get("pages") or []
    texte = "\n\n".join(page.get("markdown", "") for page in pages).strip()

    if _visible_length(texte) < USEFUL_TEXT_THRESHOLD:
        logger.info(
            "L'OCR n'a rien tiré de %s (%d pages, %d caractères).",
            path.name, len(pages), _visible_length(texte),
        )
        return texte, "aucun"

    logger.info("OCR : %d page(s) lues dans %s.", len(pages), path.name)
    return texte, "ocr"
