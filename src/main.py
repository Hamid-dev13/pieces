"""Assemblage et démarrage du bot."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError

from . import bot as bot_module
from . import config as config_module
from . import db
from .config import ConfigError


async def run() -> None:
    config = config_module.load()
    config.documents_dir.mkdir(parents=True, exist_ok=True)
    db.migrate(config.db_path)

    dispatcher = Dispatcher()
    dispatcher["config"] = config
    dispatcher.update.outer_middleware(bot_module.OwnerOnly(config.allowed_ids))
    dispatcher.include_router(bot_module.router)

    logging.info(
        "Démarrage — %d compte(s) autorisé(s), données dans %s",
        len(config.allowed_ids),
        config.data_dir,
    )

    telegram = Bot(config.token)
    try:
        # Pas de `drop_pending_updates` : un PDF envoyé pendant un redémarrage
        # doit arriver quand même. Les updates des autres comptes sont refusés
        # au passage, pas oubliés.
        await dispatcher.start_polling(telegram)
    finally:
        await telegram.session.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    try:
        asyncio.run(run())
    except ConfigError as erreur:
        # Mourir tout de suite, pas trois heures plus tard devant un document.
        logging.error("Configuration incomplète : %s", erreur)
        sys.exit(1)
    except TelegramUnauthorizedError:
        # Même raison : un jeton refusé se voit au démarrage, et une ligne
        # lisible vaut mieux qu'une trace de trente lignes dans les logs.
        logging.error("TELEGRAM_TOKEN refusé par Telegram. Jeton erroné ou révoqué.")
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
