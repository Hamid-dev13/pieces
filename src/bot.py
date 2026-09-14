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

from . import db, extract, storage
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


def extract_into_db(config: Config, sha256: str, chemin: str) -> str:
    """Extrait le texte d'un document déjà rangé et le complète en base.

    Renvoie la source retenue : 'natif', ou 'aucun' quand le PDF n'a pas de
    couche texte — ce dernier cas attend l'OCR de `ext-2`.
    """
    texte, source = extract.from_pdf(config.documents_dir / chemin)
    db.update_text(config.db_path, sha256=sha256, texte=texte, source_texte=source)
    return source


def ocr_into_db(config: Config, sha256: str, chemin: str) -> str:
    """Passe un document à l'OCR et complète la base. Renvoie la source.

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
    return source


def catch_up(config: Config) -> int:
    """Rattrape les documents rangés avant que l'extraction n'existe.

    Sans ce passage, un document arrivé pendant une panne — ou avant cette
    story — resterait muet pour toujours : le renvoyer ne servirait à rien,
    le dédoublonnage l'écarterait avant d'y toucher.
    """
    en_attente = db.awaiting_extraction(config.db_path)
    if not en_attente:
        return 0

    logger.info("Rattrapage : %d document(s) sans texte.", len(en_attente))
    traites = 0
    for document in en_attente:
        sha, chemin, fichier = document["sha256"], document["chemin"], document["fichier"]
        source = extract_into_db(config, sha, chemin)
        if source == "aucun":
            logger.info("Rattrapage par OCR : %s", fichier)
            source = ocr_into_db(config, sha, chemin)
        if source in ("aucun", extract.UNAVAILABLE):
            logger.warning("Rien à lire dans %s (%s).", fichier, source)
        else:
            traites += 1
            logger.info("Rattrapé (%s) : %s", source, fichier)
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
    source = await asyncio.to_thread(extract_into_db, config, sha, chemin)

    if source == "natif":
        await message.reply(
            f"Rangé — {filename}.\n"
            "Texte extrait. Je ne sais pas encore le classer, ça vient."
        )
        return

    # L'OCR se compte en minutes : sans ce mot, le bot paraîtrait planté.
    await message.reply(
        f"Rangé — {filename}.\n"
        "Aucun texte dedans : c'est un scan, je le déchiffre. Ça prend un moment."
    )
    source = await asyncio.to_thread(ocr_into_db, config, sha, chemin)

    if source == "ocr":
        await message.reply("Lu par OCR. Je ne sais pas encore le classer, ça vient.")
        logger.info("Document lu par OCR : %s (%s)", filename, sha[:12])
    elif source == extract.UNAVAILABLE:
        await message.reply(
            "L'OCR ne répond pas pour l'instant. Le document est rangé,\n"
            "je le relirai au prochain démarrage."
        )
        logger.warning("OCR indisponible : %s (%s)", filename, sha[:12])
    else:
        await message.reply(
            "Je n'ai rien pu en tirer, même par OCR. Le document reste rangé.\n"
            "Une photo plus nette ou mieux cadrée marcherait peut-être mieux."
        )
        logger.warning("OCR sans résultat : %s (%s)", filename, sha[:12])


@router.message()
async def anything_else(message: Message) -> None:
    await message.reply("Envoie-moi un PDF et je le range.")
