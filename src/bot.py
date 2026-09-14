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

from . import db, storage
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


def _store(payload: bytes, filename: str, config: Config) -> tuple[bool, str]:
    """Range le document. Renvoie (déjà connu, empreinte).

    Le fichier est écrit **avant** l'insertion en base : si le conteneur meurt
    entre les deux, un fichier sans ligne est inoffensif — le prochain envoi du
    même document complétera la base. L'inverse laisserait une entrée pointant
    vers un fichier absent.
    """
    sha = storage.digest(payload)
    if db.find_by_sha(config.db_path, sha) is not None:
        return True, sha

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
        return True, sha
    return False, sha


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
    already_known, sha = await asyncio.to_thread(_store, payload, filename, config)

    if already_known:
        await message.reply(f"Déjà rangé — {filename} est en double.")
        logger.info("Doublon ignoré : %s (%s)", filename, sha[:12])
    else:
        await message.reply(
            f"Rangé — {filename}.\n"
            "Je ne sais pas encore le lire ni le classer, ça vient."
        )
        logger.info("Document rangé : %s (%s)", filename, sha[:12])


@router.message()
async def anything_else(message: Message) -> None:
    await message.reply("Envoie-moi un PDF et je le range.")
