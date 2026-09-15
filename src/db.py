"""Schéma SQLite, migrations et accès.

Une connexion par opération : à une écriture par document et quelques lectures
par jour, le coût est invisible, et ça évite d'avoir à protéger une connexion
partagée entre la boucle asyncio et les threads.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

# Chaque entrée est appliquée une fois, dans l'ordre, et n'est plus jamais
# modifiée ensuite : `PRAGMA user_version` retient où on en est.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE documents (
      id             INTEGER PRIMARY KEY,
      sha256         TEXT NOT NULL UNIQUE,
      fichier        TEXT NOT NULL,
      chemin         TEXT NOT NULL,
      type           TEXT NOT NULL DEFAULT 'autre',
      titre          TEXT NOT NULL DEFAULT '',
      emetteur       TEXT,
      date_document  TEXT,
      date_expiration TEXT,
      texte          TEXT NOT NULL DEFAULT '',
      source_texte   TEXT NOT NULL DEFAULT 'aucun'
                     CHECK (source_texte IN ('aucun', 'natif', 'ocr')),
      classe_par     TEXT NOT NULL DEFAULT 'aucun'
                     CHECK (classe_par IN ('aucun', 'llm', 'humain')),
      recu_le        TEXT NOT NULL
    );

    CREATE TABLE corrections (
      id          INTEGER PRIMARY KEY,
      document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      champ       TEXT NOT NULL,
      avant       TEXT,
      apres       TEXT NOT NULL,
      corrige_le  TEXT NOT NULL
    );

    CREATE INDEX documents_type ON documents(type);
    CREATE INDEX documents_expiration ON documents(date_expiration)
      WHERE date_expiration IS NOT NULL;

    CREATE VIRTUAL TABLE documents_fts USING fts5(
      titre, emetteur, texte,
      content='documents', content_rowid='id',
      tokenize="unicode61 remove_diacritics 2"
    );

    -- En mode `content=`, FTS5 ne se met pas à jour tout seul : sans ces trois
    -- déclencheurs l'index reste vide, et ça ne se voit qu'à la recherche.
    CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
      INSERT INTO documents_fts(rowid, titre, emetteur, texte)
      VALUES (new.id, new.titre, new.emetteur, new.texte);
    END;

    CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
      INSERT INTO documents_fts(documents_fts, rowid, titre, emetteur, texte)
      VALUES ('delete', old.id, old.titre, old.emetteur, old.texte);
    END;

    CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN
      INSERT INTO documents_fts(documents_fts, rowid, titre, emetteur, texte)
      VALUES ('delete', old.id, old.titre, old.emetteur, old.texte);
      INSERT INTO documents_fts(rowid, titre, emetteur, texte)
      VALUES (new.id, new.titre, new.emetteur, new.texte);
    END;
    """,
    # L'OCR coûte des minutes, là où l'extraction native coûtait des
    # millisecondes. Sans cette trace, un scan que l'OCR ne sait pas lire
    # serait retenté à chaque redémarrage, indéfiniment.
    """
    ALTER TABLE documents ADD COLUMN ocr_tente_le TEXT;
    """,
)


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    # WAL est une propriété persistante du fichier : le passage quotidien des
    # expirations doit pouvoir lire pendant que le bot écrit.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def migrate(db_path: Path) -> None:
    """Amène la base au dernier schéma. Sans effet si elle y est déjà."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as connection:
        applied = connection.execute("PRAGMA user_version").fetchone()[0]
        for index in range(applied, len(MIGRATIONS)):
            connection.executescript(MIGRATIONS[index])
            # `user_version` n'accepte pas de paramètre lié ; la valeur est un
            # entier issu d'un range, pas d'une entrée utilisateur.
            connection.execute(f"PRAGMA user_version = {index + 1}")
            connection.commit()


def find_by_sha(db_path: Path, sha256: str) -> sqlite3.Row | None:
    with closing(connect(db_path)) as connection:
        return connection.execute(
            "SELECT * FROM documents WHERE sha256 = ?", (sha256,)
        ).fetchone()


def insert_document(
    db_path: Path, *, sha256: str, fichier: str, chemin: str, titre: str
) -> None:
    """Enregistre un document reçu, pas encore extrait ni classé.

    Les autres colonnes prennent leurs valeurs par défaut : un document connu
    mais pas encore compris.
    """
    with closing(connect(db_path)) as connection:
        connection.execute(
            """
            INSERT INTO documents (sha256, fichier, chemin, titre, recu_le)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sha256, fichier, chemin, titre, datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()


def update_text(db_path: Path, *, sha256: str, texte: str, source_texte: str) -> None:
    """Complète un document avec le texte extrait.

    Les déclencheurs FTS reprennent l'indexation au passage : le document
    devient cherchable sans rien avoir à faire de plus.
    """
    with closing(connect(db_path)) as connection:
        connection.execute(
            "UPDATE documents SET texte = ?, source_texte = ? WHERE sha256 = ?",
            (texte, source_texte, sha256),
        )
        connection.commit()


def awaiting_extraction(db_path: Path) -> list[sqlite3.Row]:
    """Les documents qu'aucune extraction n'a encore traversés.

    Un document rangé pendant que l'extraction n'existait pas, ou arrivé
    juste avant un arrêt, se retrouve ici plutôt que d'être oublié. Ceux dont
    l'OCR a déjà été tenté en sortent, quel qu'ait été le résultat : les
    repasser coûterait des minutes pour le même échec.
    """
    with closing(connect(db_path)) as connection:
        return connection.execute(
            """
            SELECT sha256, fichier, chemin FROM documents
            WHERE source_texte = 'aucun' AND ocr_tente_le IS NULL
            """
        ).fetchall()


def mark_ocr_attempted(db_path: Path, sha256: str) -> None:
    """Retient qu'on a tenté l'OCR, même quand il n'a rien donné."""
    with closing(connect(db_path)) as connection:
        connection.execute(
            "UPDATE documents SET ocr_tente_le = ? WHERE sha256 = ?",
            (datetime.now(timezone.utc).isoformat(), sha256),
        )
        connection.commit()


def update_classification(
    db_path: Path,
    *,
    sha256: str,
    type: str,
    titre: str,
    emetteur: str | None,
    date_document: str | None,
    date_expiration: str | None,
) -> None:
    """Complète un document avec ce que le modèle y a lu.

    `classe_par` passe à 'llm', ce qui sort le document du rattrapage. Un titre
    vide n'écrase pas celui qui est en base : le nom du fichier reste un
    meilleur repère que rien.

    Les déclencheurs FTS réindexent titre et émetteur au passage — c'est par là
    que `rec-1` retrouvera un document par son titre plutôt que par son texte.
    """
    with closing(connect(db_path)) as connection:
        connection.execute(
            """
            UPDATE documents SET
              type = ?,
              titre = CASE WHEN ? = '' THEN titre ELSE ? END,
              emetteur = ?,
              date_document = ?,
              date_expiration = ?,
              classe_par = 'llm'
            WHERE sha256 = ?
            """,
            (type, titre, titre, emetteur, date_document, date_expiration, sha256),
        )
        connection.commit()


def awaiting_classification(db_path: Path) -> list[sqlite3.Row]:
    """Les documents lisibles qu'aucun classement n'a encore traversés.

    Deux conditions, et la seconde compte autant que la première : classer un
    document dont on n'a pas le texte reviendrait à demander au modèle de
    deviner à partir d'un nom de fichier. Il resterait ici indéfiniment, à
    chaque démarrage — alors qu'en attendant son texte, il en sortira le jour
    où l'OCR saura le lire.

    Pas de colonne « classement tenté », contrairement à l'OCR : un classement
    est local, gratuit, et se compte en secondes. Un document que le modèle ne
    sait pas nommer atterrit dans `autre` avec `classe_par = 'llm'`, donc hors
    de cette liste ; seule une panne d'Ollama l'y laisse, et c'est exactement
    ce qu'on veut reprendre.
    """
    with closing(connect(db_path)) as connection:
        return connection.execute(
            """
            SELECT sha256, fichier, texte FROM documents
            WHERE classe_par = 'aucun' AND source_texte <> 'aucun'
            """
        ).fetchall()
