import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy import text

import data_sources
from config import DB_SCHEMA

log = logging.getLogger(__name__)

METADATA_PATH = Path("./schema_metadata.json")

# Matches audit columns whether bare (created_at) or table-prefixed (block_created_by, oph_updated_date).
_AUDIT_RE = re.compile(
    r"(?:^|_)(created|updated|modified|deleted)_(by|at|date|time|timestamp)$",
    re.IGNORECASE,
)

_DATE_COL_RE = re.compile(r"(date|time)", re.IGNORECASE)

_TYPE_MAP: dict[str, str] = {
    "character varying": "varchar",
    "character": "varchar",
    "text": "varchar",
    "integer": "int",
    "smallint": "int",
    "bigint": "bigint",
    "numeric": "dec",
    "decimal": "dec",
    "double precision": "dec",
    "real": "dec",
    "boolean": "bool",
    "date": "date",
    "timestamp without time zone": "ts",
    "timestamp with time zone": "ts",
    # SQL Server (the staging source)
    "nvarchar": "varchar",
    "nchar": "varchar",
    "char": "varchar",
    "float": "dec",
    "money": "dec",
    "bit": "bool",
    "datetime": "ts",
    "datetime2": "ts",
    "smalldatetime": "ts",
    "tinyint": "int",
}


@dataclass
class ColumnSpec:
    name: str
    data_type: str
    is_nullable: bool
    is_pk: bool
    fk_ref: tuple[str, str] | None = None


@dataclass
class TableCard:
    name: str
    description: str
    synonyms: list[str]
    example_questions: list[str]
    columns: list[ColumnSpec] = field(default_factory=list)
    # v2 fields — all optional, default to empty
    domain_tags: list[str]                = field(default_factory=list)
    key_columns: dict[str, str]           = field(default_factory=dict)
    common_joins: list[dict]              = field(default_factory=list)
    value_hints: dict[str, list[str]]     = field(default_factory=dict)
    row_estimate: str                     = ""


def embedding_text(card: TableCard) -> str:
    col_names = ", ".join(c.name for c in card.columns[:15])
    syns      = ", ".join(card.synonyms) if card.synonyms else ""
    examples  = " | ".join(card.example_questions) if card.example_questions else ""
    tags      = ", ".join(card.domain_tags) if card.domain_tags else ""
    joins     = ", ".join(j.get("to", "") for j in card.common_joins) if card.common_joins else ""

    parts = [f"{card.name} — {card.description}"]
    if tags:
        parts.append(f"Tags: {tags}")
    if col_names:
        parts.append(f"Columns: {col_names}")
    if syns:
        parts.append(f"Synonyms: {syns}")
    if examples:
        parts.append(f"Examples: {examples}")
    if joins:
        parts.append(f"Joins: {joins}")
    return "\n".join(parts)


def _type_alias(data_type: str) -> str:
    return _TYPE_MAP.get(data_type.lower(), data_type[:10])


def _is_varchar_date(col_name: str, data_type: str) -> bool:
    if data_type.lower() in ("character varying", "text", "character",
                             "varchar", "nvarchar", "char", "nchar"):
        return bool(_DATE_COL_RE.search(col_name))
    return False


def compact_ddl_for(table_names: list[str], *, include_descriptions: bool = False) -> str:
    """Return compact one-line-per-table DDL, tables sorted alphabetically.

    If include_descriptions is True, key-column descriptions from schema_metadata.json
    are appended inline as "# desc". Off by default to preserve token cost on fast-mode
    SQL generation. Useful for reasoning mode or hard questions.
    """
    curated = data_sources.active().curated_ddl
    lines = []
    for name in sorted(table_names):
        card = cards().get(name)
        if card is None:
            continue
        col_parts = []
        for col in card.columns:
            if curated and card.key_columns and col.name not in card.key_columns:
                continue
            # Audit columns are dropped to save tokens — EXCEPT those curated in
            # key_columns (e.g. t_oph.oph_created_date, which business rule 5
            # mandates for harvest date filters; hiding it made that rule
            # un-followable and the model silently fell back to
            # oph_approved_date).
            is_audit = (bool(_AUDIT_RE.search(col.name))
                        and col.name not in card.key_columns)
            if is_audit and not col.is_pk and col.fk_ref is None:
                continue
            if col.is_pk:
                base = f"{col.name} PK"
            elif col.fk_ref:
                ref_table, ref_col = col.fk_ref
                base = f"{col.name}→{ref_table}.{ref_col}"
            else:
                alias = _type_alias(col.data_type)
                date_hint = "[date]" if _is_varchar_date(col.name, col.data_type) else ""
                base = f"{col.name} {alias}{date_hint}"

            if include_descriptions or curated:
                desc = card.key_columns.get(col.name)
                if desc:
                    base = f"{base} # {desc}"

            col_parts.append(base)
        lines.append(f"{name}({', '.join(col_parts)})")
    return "\n".join(lines)


# Table cards per data source, populated at server startup by load_all_cards().
# Every reader goes through cards(), which resolves the source in force for the
# current request (data_sources.active()); the live databases are the default.
_CARDS: dict[str, dict[str, TableCard]] = {}
_CACHE_LOCK = Lock()


def cards(source_id: str | None = None) -> dict[str, TableCard]:
    return _CARDS.get(source_id or data_sources.active().id, {})


def _load_metadata_json(path: Path = METADATA_PATH) -> dict[str, Any]:
    """Load a schema metadata file. Supports both v1 (flat) and v2 (nested under 'tables') layouts.

    Returns a flat dict[table_name -> table_meta] regardless of input version.
    """
    if not path.exists():
        print(
            f"ERROR: {path.name} not found. "
            "Run 'python bootstrap_metadata.py' first to generate schema_metadata.json.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    meta_block = data.get("_meta") if isinstance(data, dict) else None
    if isinstance(meta_block, dict) and "tables" in data:
        version = meta_block.get("schema_version", 1)
        log.info("schema_metadata.json schema_version=%s, generator=%s",
                 version, meta_block.get("generator", "unknown"))
        return data["tables"]

    # v1 fallback — flat top-level dict of table_name -> entry
    log.info("schema_metadata.json appears to be v1 (flat). Reading as-is.")
    return data


def _fetch_db_schema(schema: str, engine) -> tuple[
    dict[str, list[dict]],
    dict[str, set[str]],
    dict[str, dict[str, tuple[str, str]]],
]:
    """Fetch columns, PKs, and FKs from information_schema. Returns three dicts."""
    col_sql = text("""
        SELECT table_name AS table_name, column_name AS column_name,
               data_type AS data_type, is_nullable AS is_nullable
        FROM information_schema.columns
        WHERE table_schema = :schema
        ORDER BY table_name, ordinal_position
    """)
    pk_sql = text("""
        SELECT kcu.table_name AS table_name, kcu.column_name AS column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = :schema
    """)
    fk_sql = text("""
        SELECT
            kcu.table_name  AS table_name,
            kcu.column_name AS column_name,
            ccu.table_name  AS foreign_table_name,
            ccu.column_name AS foreign_column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = :schema
    """)

    with engine.connect() as conn:
        col_rows = conn.execute(col_sql, {"schema": schema}).fetchall()
        pk_rows  = conn.execute(pk_sql,  {"schema": schema}).fetchall()
        fk_rows  = conn.execute(fk_sql,  {"schema": schema}).fetchall()

    cols_by_table: dict[str, list[dict]] = {}
    for r in col_rows:
        cols_by_table.setdefault(r.table_name, []).append({
            "name": r.column_name,
            "data_type": r.data_type,
            "is_nullable": r.is_nullable == "YES",
        })

    pks_by_table: dict[str, set[str]] = {}
    for r in pk_rows:
        pks_by_table.setdefault(r.table_name, set()).add(r.column_name)

    fks_by_table: dict[str, dict[str, tuple[str, str]]] = {}
    for r in fk_rows:
        fks_by_table.setdefault(r.table_name, {})[r.column_name] = (
            r.foreign_table_name,
            r.foreign_column_name,
        )

    return cols_by_table, pks_by_table, fks_by_table


def load_all_cards(
    schema: str | None = None, source: "data_sources.DataSource | str | None" = None,
) -> dict[str, TableCard]:
    """Merge a source's metadata JSON with its live information_schema data.

    `source` defaults to the live Postgres databases. What is hidden from the
    retrieval index is the source's own rule (data_sources.DataSource.excludes):
    for live the SAP ZEPMS staging tables and the unused/coconut tables, for
    staging everything the metadata file doesn't describe.
    Exits if the metadata JSON is missing.
    """
    if source is None:
        source = data_sources.LIVE
    src = source if isinstance(source, data_sources.DataSource) else data_sources.get(source)
    schema = schema or (DB_SCHEMA if src.id == data_sources.LIVE else src.schema)

    metadata = _load_metadata_json(src.metadata_path)
    cols_by_table, pks_by_table, fks_by_table = _fetch_db_schema(schema, src.engine)

    def _hidden(name: str) -> bool:
        return src.excludes(name) or (src.only_described and name not in metadata)

    hidden = sorted(n for n in cols_by_table if _hidden(n))
    if hidden:
        log.info("[%s] hiding %d tables from the retrieval index: %s", src.id, len(hidden), hidden)
    cols_by_table = {k: v for k, v in cols_by_table.items() if not _hidden(k)}
    pks_by_table  = {k: v for k, v in pks_by_table.items()  if not _hidden(k)}
    fks_by_table  = {k: v for k, v in fks_by_table.items()  if not _hidden(k)}
    metadata      = {k: v for k, v in metadata.items()      if not src.excludes(k)}

    db_tables   = set(cols_by_table.keys())
    meta_tables = set(metadata.keys())

    new_tables = db_tables - meta_tables
    if new_tables:
        log.warning(
            "[%s] tables in the database not in %s: %s - "
            "Run bootstrap_metadata.py to add them.",
            src.id, src.metadata_path.name, sorted(new_tables),
        )

    orphaned = meta_tables - db_tables
    if orphaned:
        log.warning(
            "[%s] orphaned entries in %s (no matching table): %s",
            src.id, src.metadata_path.name, sorted(orphaned),
        )

    cards: dict[str, TableCard] = {}
    for table_name, db_cols in cols_by_table.items():
        meta = metadata.get(table_name, {})
        pk_set = pks_by_table.get(table_name, set())
        fk_map = fks_by_table.get(table_name, {})

        columns = [
            ColumnSpec(
                name=c["name"],
                data_type=c["data_type"],
                is_nullable=c["is_nullable"],
                is_pk=c["name"] in pk_set,
                fk_ref=fk_map.get(c["name"]),
            )
            for c in db_cols
        ]

        cards[table_name] = TableCard(
            name=table_name,
            description=meta.get("description", f"Table {table_name}"),
            synonyms=meta.get("synonyms", []),
            example_questions=meta.get("example_questions", []),
            columns=columns,
            domain_tags=meta.get("domain_tags", []),
            key_columns=meta.get("key_columns", {}),
            common_joins=meta.get("common_joins", []),
            value_hints=meta.get("value_hints", {}),
            row_estimate=meta.get("row_estimate", ""),
        )

    with _CACHE_LOCK:
        _CARDS[src.id] = cards

    _validate_metadata(cards, cols_by_table)

    log.info("[%s] loaded %d table cards (%s + information_schema)",
             src.id, len(cards), src.metadata_path.name)
    return cards


def _validate_metadata(cards: dict[str, TableCard], db_cols: dict[str, list[dict]]) -> None:
    """Fail-soft validation of v2 metadata fields. Logs warnings only."""
    empty_descs:    list[str] = []
    bad_keycols:    list[str] = []
    bad_joins:      list[str] = []
    valid_tables = set(db_cols.keys())

    for name, card in cards.items():
        if not card.description or card.description == f"Table {name}":
            empty_descs.append(name)

        col_set = {c.name for c in card.columns}
        for kc in card.key_columns.keys():
            if kc not in col_set:
                bad_keycols.append(f"{name}.{kc}")

        for j in card.common_joins:
            tgt = j.get("to")
            if tgt and tgt not in valid_tables:
                bad_joins.append(f"{name}->{tgt}")

    if empty_descs:
        log.warning("Tables with empty/default descriptions: %d (e.g. %s)",
                    len(empty_descs), empty_descs[:5])
    if bad_keycols:
        log.warning("key_columns referencing missing DB columns: %s", bad_keycols[:10])
    if bad_joins:
        log.warning("common_joins to non-existent tables: %s", bad_joins[:10])


def get_card(name: str) -> TableCard | None:
    return cards().get(name)


def reload_cards(schema: str | None = None, source: str | None = None) -> dict[str, TableCard]:
    """Force-reload from DB and JSON. Called by /admin/reload-schema."""
    log.info("/admin/reload-schema triggered — reloading table cards")
    return load_all_cards(schema, source)
