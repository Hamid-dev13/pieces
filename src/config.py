"""Configuration lue dans l'environnement, validée au démarrage.

Un bot lancé sans son jeton doit mourir tout de suite, pas trois heures plus
tard devant un document.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# L'API Bot refuse de servir un fichier au-delà de 20 Mo, quoi qu'on fasse.
MAX_FILE_BYTES = 20 * 1024 * 1024


class ConfigError(RuntimeError):
    """Une variable manque ou ne veut rien dire."""


@dataclass(frozen=True)
class Config:
    token: str
    allowed_ids: frozenset[int]
    data_dir: Path
    mistral_api_key: str

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pieces.db"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} est vide ou absent.")
    return value


def _parse_ids(raw: str) -> frozenset[int]:
    ids = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            raise ConfigError(
                f"PIECES_ALLOWED_IDS : « {chunk} » n'est pas un identifiant "
                "numérique. Telegram n'accepte pas les @pseudo ici."
            ) from None
    if not ids:
        raise ConfigError("PIECES_ALLOWED_IDS ne contient aucun identifiant.")
    return frozenset(ids)


def load() -> Config:
    """Lit l'environnement, ou lève ConfigError."""
    return Config(
        token=_required("TELEGRAM_TOKEN"),
        allowed_ids=_parse_ids(_required("PIECES_ALLOWED_IDS")),
        data_dir=Path(os.environ.get("PIECES_DATA_DIR", "/data")),
        # Exigée au démarrage, comme le reste : sans elle, les scans
        # s'empileraient sans être lus, et on s'en apercevrait des mois plus
        # tard devant un document introuvable.
        mistral_api_key=_required("MISTRAL_API_KEY"),
    )
