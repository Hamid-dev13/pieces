"""Écriture des PDF dans le volume.

Le nom du fichier **est** son empreinte : le dédoublonnage devient une
propriété du système de fichiers, pas une règle à faire respecter.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

PDF_MAGIC = b"%PDF-"


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def looks_like_pdf(payload: bytes) -> bool:
    """Un .pdf qui n'en est pas un se voit à ses premiers octets."""
    return payload.startswith(PDF_MAGIC)


def relative_path(sha: str) -> str:
    """Chemin relatif au volume, tel qu'il est gardé en base."""
    return f"{sha[:2]}/{sha}.pdf"


def save(payload: bytes, sha: str, documents_dir: Path) -> str:
    """Écrit le PDF s'il n'y est pas déjà, et renvoie son chemin relatif.

    L'écriture passe par un temporaire puis un rename : un fichier présent
    sous son nom définitif est toujours complet, même si le conteneur meurt
    au milieu.
    """
    relative = relative_path(sha)
    final = documents_dir / relative
    if final.exists():
        return relative

    final.parent.mkdir(parents=True, exist_ok=True)
    temporary = final.with_name(f"{sha}.part")
    temporary.write_bytes(payload)
    os.replace(temporary, final)
    return relative
