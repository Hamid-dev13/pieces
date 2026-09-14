"""Extraction du texte des PDF.

`pdftotext` d'abord. Quand il rend trop peu de texte, le document est un scan :
l'OCR prendra le relais (story `ext-2`), et `source_texte` gardera la trace du
chemin emprunté — utile quand un document est mal classé, pour savoir si c'est
l'extraction ou le modèle qui a fauté.
"""

from __future__ import annotations

import logging
import subprocess
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
