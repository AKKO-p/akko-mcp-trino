"""Garde-fous SQL — vendor-neutre, sans aucune dépendance AKKO.

Extrait à l'IDENTIQUE de server.py (P1 refactor pur) : échappement de littéraux,
validation d'identifiants, classification read-only par préfixe. Logique pure et
testable (zéro I/O). La version P2 remplacera la classification par préfixe par une
analyse AST (sqlglot) ; ici on préserve le comportement exact.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

# Identifiant Trino : alphanumérique, underscore, tiret uniquement.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

# Types d'expression sqlglot purement en LECTURE.
_READ_TYPES = (exp.Select, exp.Union, exp.Describe, exp.Pragma)
# SHOW/EXPLAIN/DESC tombent en `Command` générique (syntaxe non structurée par
# sqlglot) — on les autorise par mot-clé de tête (read seulement).
_COMMAND_READ_HEADS = ("SHOW", "EXPLAIN", "DESCRIBE", "DESC")


def safe_sql_string(text: str) -> str:
    """Échappe une chaîne utilisateur pour inclusion sûre dans un littéral SQL Trino.

    Gère apostrophes, antislashs et octets nuls. Le résultat est destiné à être
    entouré d'apostrophes par l'appelant : ``f"'{safe_sql_string(val)}'"``.
    """
    text = text.replace("\0", "")
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "''")
    return text


def validate_identifier(name: str, label: str = "identifier") -> str:
    """Valide et renvoie un identifiant SQL Trino, ou lève ValueError.

    Seuls alphanumériques, underscores et tirets sont admis (anti-injection sur
    les noms de catalogue/schéma/table).
    """
    if not name or not _IDENTIFIER_RE.match(name):
        raise ValueError(
            f"Invalid {label}: {name!r}. "
            "Only alphanumeric characters, underscores, and hyphens are allowed."
        )
    return name


def is_read_only_sql(sql: str) -> bool:
    """True SSI la requête est purement en lecture, par analyse AST (sqlglot).

    Plus strict et plus sûr que l'ancien filtre par préfixe : rejette le
    multi-statement (ex. ``SELECT 1; DROP TABLE t``), les WITH qui enveloppent une
    écriture, et les commandes à effet de bord (GRANT, CALL, SET…). Fail-closed : une
    requête non analysable est refusée.
    """
    s = sql.strip()
    if not s:
        return False
    try:
        statements = [st for st in sqlglot.parse(s, read="trino") if st is not None]
    except Exception:  # noqa: BLE001 — parse impossible → refus (fail-closed)
        return False
    if len(statements) != 1:  # un seul statement, jamais de requêtes empilées
        return False
    st = statements[0]
    if isinstance(st, _READ_TYPES):
        return True
    if isinstance(st, exp.Command):  # SHOW / EXPLAIN / DESC non structurés
        head = s.upper().split(None, 1)[0]
        return head in _COMMAND_READ_HEADS
    return False  # Insert / Update / Delete / Create / Drop / Alter / Grant / …
