"""
Which database a question runs against.

Two databases can answer a chat question:

  live     The Postgres EPMS estate databases the app was built on (BA + K3, one
           database per estate, identical schemas, readable table names such as
           t_oph / m_block).
  staging  The central SQL Server EPMS staging database. Every estate sits in one
           database (EA, EB, EC and plasma schemes P1/P2, told apart by the WERKS
           column) and the tables keep the SAP-style codes (OPH, FDN, BLK ...).

They differ in dialect (Postgres vs T-SQL), schema, metadata, business rules and
the way a question is scoped to an estate, so each is described by one
DataSource. The chat pipeline asks `active()` for the one in force instead of
importing the engine directly; `use("staging")` switches it for the duration of
a request via a ContextVar (the same pattern as llm_client.model_override), so
two concurrent requests on different databases never see each other.

The default is "live", which is what every module that has never heard of this
file keeps using.
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import config

LIVE = "live"
STAGING = "staging"

_ROOT = Path(__file__).parent


@dataclass
class DataSource:
    id: str
    label: str
    description: str
    dialect: str                       # "postgresql" | "mssql"
    schema: str                        # schema whose tables are indexed
    engine: object | None              # primary SQLAlchemy engine (None = not configured)
    scopes: dict[str, dict]            # scope id -> {"label", "engine"}; fan-out units
    scope_order: list[str]
    metadata_path: Path
    rules_path: Path
    preamble_prompt: str               # prompts/<name>.txt
    tables_collection: str             # Chroma collection names, one set per source
    history_collection: str
    excludes: Callable[[str], bool] = lambda name: False
    # Staging tables nobody has described are noise to retrieval ("Table GWO"),
    # so only tables that appear in the metadata file are indexed.
    only_described: bool = False
    # Show the SQL writer only the curated key columns of each table, each with
    # its description inline. The staging tables are wide (OPH has 44 columns,
    # most of them carrier/distance slots) and cryptically named (ZMASAK), so a
    # full dump invites wrong columns where the live tables name themselves.
    curated_ddl: bool = False
    # SQL returning the date the data runs to (one value), for sources that are
    # loaded in batches. Lets an empty "yesterday" be explained instead of
    # read as "nothing happened".
    freshness_sql: Optional[str] = None
    # For the chat page: what the database covers (plain words, used when the
    # user asks what the assistant can do) and questions for the empty state.
    topics: str = ""
    # A check on generated SQL for mistakes that run fine but give wrong
    # numbers. lint(sql, question) returns a message for the SQL writer, or None
    # when the query is sound. A flagged query is not run: it goes back for one
    # correction, and it is never kept as an example for later questions.
    lint: Optional[Callable[[str, str], Optional[str]]] = None
    starters: list[str] = field(default_factory=list)
    entities: dict[str, dict] = field(default_factory=dict)  # staging: estate code -> {werks,label,aliases}

    @property
    def available(self) -> bool:
        return self.engine is not None

    def data_through(self) -> Optional[str]:
        """ISO date the source's data runs to, cached for 30 minutes; None if unknown."""
        if not self.freshness_sql or not self.available:
            return None
        now = time.monotonic()
        hit = _freshness.get(self.id)
        if hit and now - hit[0] < 1800:
            return hit[1]
        value = None
        try:
            from sqlalchemy import text
            with self.engine.connect() as conn:
                v = conn.execute(text(self.freshness_sql)).scalar()
            value = str(v) if v is not None else None
        except Exception:
            value = hit[1] if hit else None
        _freshness[self.id] = (now, value)
        return value

    # -- estate scoping -----------------------------------------------------

    def entity_labels(self) -> list[str]:
        """Estate names to show the clarify gate and the UI."""
        if self.entities:
            return [e["label"] for e in self.entities.values()]
        return [self.scopes[s]["label"] for s in self.scope_order]

    def detect_entities(self, text: str) -> list[str]:
        """Entity codes (staging) named in the text, in registry order."""
        out: list[str] = []
        for code, ent in self.entities.items():
            if ent["pattern"].search(text or ""):
                out.append(code)
        return out

    def scope_hint(self, codes: list[str]) -> Optional[str]:
        """Line appended to the SQL writer's question to scope it to estates.

        The live databases are scoped by choosing which engines to run on; the
        staging database holds every estate, so the scope has to be a WHERE.
        `codes` are entity codes (EA, EC, ...) named in the message or carried
        from the turn before. Returns None for no estate (= all of them).
        """
        codes = [c for c in codes if c in self.entities]
        if not codes:
            return None
        ents = [self.entities[c] for c in codes]
        names = ", ".join(e["label"] for e in ents)
        werks = ", ".join(f"'{e['werks']}'" for e in ents)
        estnr = ", ".join(f"'{c}'" for c in codes)
        prfnr = ", ".join(f"'{e['prfnr']}'" for e in ents)
        return (f"Scope: only {names}. Filter with WERKS IN ({werks}) on OPH, GWO and OVT; "
                f"ESTNR IN ({estnr}) on FDN, BLK, DIV and EST; "
                f"PRFNR IN ({prfnr}) on ATD, EMP and MEM.")


def _compile(aliases: list[str]) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(re.escape(a) for a in aliases) + r")\b", re.IGNORECASE)


# Staging estate codes. WERKS is BUKRS ('00') + ESTNR; EST.WERKS carries it.
# code -> (WERKS, label, PRFNR name as stored in the tables, aliases)
_STAGING_ENTITIES = {
    "EA": ("00EA", "Estate A (EA)", "ESTATE A", ["ea", "estate a", "kebun a"]),
    "EB": ("00EB", "Estate B (EB)", "ESTATE B", ["eb", "estate b", "kebun b"]),
    "EC": ("00EC", "Estate C (EC)", "ESTATE C", ["ec", "estate c", "kebun c"]),
    # Tables that carry the estate as text write the plasma schemes with the
    # ESTATE prefix ("ESTATE PLASMA 1"); only EST.NAME1 drops it.
    "P1": ("00P1", "Plasma 1 (P1)", "ESTATE PLASMA 1", ["p1", "plasma 1", "plasma1"]),
    "P2": ("00P2", "Plasma 2 (P2)", "ESTATE PLASMA 2", ["p2", "plasma 2", "plasma2"]),
}


# Soft-deleted rows (business rule 3): table -> its flag column. A query that
# reads the table must either filter on the flag or be asking about deleted rows.
_SOFT_DELETE = {"t_oph": "oph_is_deleted", "t_fdn": "fdn_is_deleted", "t_cp": "cp_is_deleted"}
_ABOUT_DELETED = re.compile(r"\b(?:delet\w*|remov\w*|dihapus|terhapus|hapus|dibatalkan)\b", re.IGNORECASE)
_ABOUT_APPROVAL = re.compile(r"\b(?:approv\w*|disetujui|persetujuan|disahkan)\b", re.IGNORECASE)


def _reads(sql: str, table: str) -> bool:
    return bool(re.search(r"\b(?:FROM|JOIN)\s+(?:\w+\.)?\"?" + table + r"\b", sql, re.IGNORECASE))


# ── block area summed over transaction rows ────────────────────────────────
# A block's area joined onto its harvest tickets repeats once per ticket, so
# SUM(area) at that level adds each block thousands of times: "1.11 bunches per
# hectare" for Estate C, from 13 million hectares. The area has to be summed in
# a query of its own that reads only the block table.

def _flatten(text: str) -> str:
    """The text with every parenthesised part emptied: only this level's words."""
    prev = None
    while prev != text:      # innermost first; the mark holds no parenthesis or word character
        prev, text = text, re.sub(r"\([^()]*\)", " § ", text)
    return text


def _levels_summing(sql: str, area_sum: re.Pattern) -> list[str]:
    """For each SUM(area) in the SQL, the text of the SELECT it belongs to,
    flattened to that level, so its FROM / JOIN show which tables feed it."""
    out = []
    for m in area_sum.finditer(sql):
        start, end, depth = 0, len(sql), 0
        # Walk outwards from the SUM to the nearest parentheses holding a SELECT.
        i = m.start()
        while i > 0:
            i -= 1
            ch = sql[i]
            if ch == ")":
                depth += 1
            elif ch == "(":
                if depth:
                    depth -= 1
                    continue
                if re.match(r"\(\s*(?:SELECT|WITH)\b", sql[i:], re.IGNORECASE):
                    start, close, d = i + 1, i, 0
                    for j in range(i, len(sql)):
                        d += sql[j] == "("
                        d -= sql[j] == ")"
                        if d == 0:
                            close = j
                            break
                    end = close
                    break
        out.append(_flatten(sql[start:end]))
    return out


def _tables_at(level: str) -> list[str]:
    return [t.lower() for t in re.findall(
        r"\b(?:FROM|JOIN)\s+(?:\w+\.)?[\[\"]?(\w+)", level, re.IGNORECASE)]


def _area_fanout(sql: str, area_sum: re.Pattern, is_transaction) -> Optional[str]:
    for level in _levels_summing(sql, area_sum):
        hit = [t for t in _tables_at(level) if is_transaction(t)]
        if hit:
            return hit[0]
    return None


_STAGING_AREA_SUM = re.compile(
    r"\bSUM\s*\(\s*(?:DISTINCT\s+)?(?:(?:TRY_)?CAST\s*\(\s*)?(?:\w+\.)?\[?BHA\]?\b", re.IGNORECASE)
_STAGING_TRANSACTIONS = {"oph", "fdn", "gwo", "atd", "ovt"}
# WERKS is '00EC', ESTNR and RIGHT(WERKS, 2) are 'EC': equating the two kinds matches nothing.
_W = r"(?:\w+\.)?\[?WERKS\]?"
_SHORT = r"(?:RIGHT\s*\(\s*" + _W + r"\s*,\s*2\s*\)|(?:\w+\.)?\[?ESTNR\]?)"
_KEY_MISMATCH = re.compile(
    r"(?:" + _SHORT + r"\s*=\s*" + _W + r"(?!\w)|" + _W + r"\s*=\s*" + _SHORT + r")", re.IGNORECASE)
_NAME_TO_PRFNR = re.compile(
    r"(?:\w+\.)?\[?NAME1\]?\s*=\s*(?:\w+\.)?\[?PRFNR\b|(?:\w+\.)?\[?PRFNR\]?\s*=\s*(?:\w+\.)?\[?NAME1\b",
    re.IGNORECASE)
_LIVE_AREA_SUM = re.compile(
    r"\bSUM\s*\(\s*(?:DISTINCT\s+)?(?:CAST\s*\(\s*)?(?:\w+\.)?\"?(?:block_hectarage|BHA)\b", re.IGNORECASE)


def _area_message(table: str, fix: str) -> str:
    return (f"Wrong result: the block area is summed over the rows of {table}, so each block's area is "
            f"added once per {table} row and the hectares come out thousands of times too large (any "
            f"per-hectare figure far too small). " + fix)


def _live_lint(sql: str, question: str = "") -> Optional[str]:
    """The two business rules the model keeps breaking, checked in code.

    Both give a plausible-looking number that is simply wrong: the harvest date
    is oph_created_date (oph_approved_date is when a ticket was approved, often
    weeks later, so a year's total shifts by double-digit percent), and rows
    flagged is_deleted must not be counted. Checked on every query, before it
    runs and before it can be saved as an example for later ones.
    """
    if _reads(sql, "t_oph") and re.search(r"\boph_approved_date\b", sql, re.IGNORECASE) \
            and not _ABOUT_APPROVAL.search(question or ""):
        return ("Wrong result: oph_approved_date is when a harvest ticket was approved, not when it was "
                "harvested. Business rule 5: for any date or period filter on t_oph (a year, month, quarter, "
                "last N days) use oph_created_date, the harvest date. Use oph_approved_date nowhere.")
    if not _ABOUT_DELETED.search(question or ""):
        for table, flag in _SOFT_DELETE.items():
            if _reads(sql, table) and not re.search(r"\b" + flag + r"\b", sql, re.IGNORECASE):
                return (f"Wrong result: {table} has soft-deleted rows ({flag} = 1) that must not be counted. "
                        f"Business rule 3: add ({flag} = 0 OR {flag} IS NULL) to the WHERE of every query "
                        f"that reads {table}, in every sub-select.")
    table = _area_fanout(sql, _LIVE_AREA_SUM, lambda t: t.startswith("t_"))
    if table:
        return _area_message(table, (
            "Sum the area in its own subquery that reads only m_block, e.g. (SELECT SUM(block_hectarage) "
            "FROM m_block WHERE block_state = 'MATURE') AS ha, sum the other measure in another, and divide "
            "the two totals."))
    return None


_FDN_REF = re.compile(r"\b(?:FROM|JOIN)\s+(?:dbo\.)?\[?FDN\]?\b", re.IGNORECASE)
_OPH_REF = re.compile(r"\b(?:FROM|JOIN)\s+(?:dbo\.)?\[?OPH\]?\b", re.IGNORECASE)
_WEIGHT_COL = re.compile(r"\b(?:WB_QTY_TOTAL|BRUTO|TARRA)\b", re.IGNORECASE)
_RAW_WEIGHT_SUM = re.compile(
    r"\b(?:SUM|AVG)\s*\(\s*(?:CAST\s*\(\s*)?(?:\w+\.)?(?:WB_QTY_TOTAL|BRUTO|TARRA)\b", re.IGNORECASE)


def _staging_lint(sql: str, question: str = "") -> Optional[str]:
    """Delivery weight is stored once per trip (SPB) but repeated on every FDN
    row of the trip. Summing it over rows, or over a join that brings those
    rows back, overstates tonnage many times while looking perfectly valid."""
    if _KEY_MISMATCH.search(sql):
        # Runs fine and matches nothing: the answer then reports "no data".
        return ("Wrong result: a join compares a 4-character WERKS ('00EC') with a 2-character code "
                "('EC'), so it matches no row. WERKS = WERKS for two transaction tables or EST; "
                "RIGHT(WERKS, 2) = ESTNR to reach BLK, DIV, FDN or EST.ESTNR. Never RIGHT(WERKS, 2) = WERKS "
                "and never ESTNR = WERKS.")
    if _NAME_TO_PRFNR.search(sql):
        # Matches Estate A to C and silently drops both plasmas.
        return ("Wrong result: EST.NAME1 says 'PLASMA 1' but PRFNR says 'ESTATE PLASMA 1', so joining "
                "NAME1 = PRFNR drops both plasma estates without an error. Do not join EST for ATD, EMP, MEM, "
                "GWO or OVT: group by PRFNR itself and show it as the estate name.")
    table = _area_fanout(sql, _STAGING_AREA_SUM, lambda t: t in _STAGING_TRANSACTIONS)
    if table:
        # Block area (BHA) joined onto transaction rows repeats the same way.
        return _area_message(table.upper(), (
            "Sum BHA in its own subquery that reads only BLK: (SELECT ESTNR, SUM(BHA) AS ha FROM BLK "
            "WHERE KDATE = '9999-12-31' GROUP BY ESTNR) a; aggregate the transactions in another subquery "
            "grouped by WERKS, then join the two on a.ESTNR = RIGHT(h.WERKS, 2) and divide."))
    if not _WEIGHT_COL.search(sql):
        return None
    fix = ("Take the weight once per trip: FROM (SELECT ESTNR, SPB, MIN(BUDAT) AS d, "
           "MAX(WB_QTY_TOTAL) AS w FROM FDN WHERE ... GROUP BY ESTNR, SPB) t, then SUM(t.w) "
           "and group by t.ESTNR / t.d only. FDN must appear exactly once, inside that subquery.")
    if _RAW_WEIGHT_SUM.search(sql):
        return ("Wrong result: WB_QTY_TOTAL is the weight of the whole trip repeated on every "
                "row of its SPB, so SUM/AVG over FDN rows counts each trip many times. " + fix)
    if len(_FDN_REF.findall(sql)) > 1 or _OPH_REF.search(sql):
        return ("Wrong result: the per-trip weight subquery is joined back to FDN (or to OPH), "
                "which repeats each trip's weight once per harvest ticket. " + fix)
    return None


def _live_excludes(name: str) -> bool:
    """Tables hidden from the live retrieval index.

    - SAP temporary staging tables (ZEPMS prefix): non-semantic column names.
    - t_abw (unused here) and the coconut-only logistics tables.
    - Everything coconut: this dataset is palm oil only.
    """
    if name.upper().startswith("ZEPMS"):
        return True
    if name in {"t_abw", "t_checkpoint", "t_checkpoint_detail", "t_checkpoint_loader"}:
        return True
    return "coconut" in name.lower()


_registry: dict[str, DataSource] | None = None
_freshness: dict[str, tuple[float, Optional[str]]] = {}


def _build() -> dict[str, DataSource]:
    live = DataSource(
        id=LIVE,
        label="Live estates (BA, K3)",
        description="PostgreSQL EPMS databases, one per estate.",
        dialect="postgresql",
        schema=config.DB_SCHEMA,
        engine=config.engine,
        scopes=config.ESTATES,
        scope_order=config.ESTATE_ORDER,
        metadata_path=_ROOT / "schema_metadata.json",
        rules_path=_ROOT / "business_rules.txt",
        preamble_prompt="sql_system_preamble",
        tables_collection="tables",
        history_collection="query_history",
        excludes=_live_excludes,
        lint=_live_lint,
        topics=("harvest records (ripe and unripe bunches by block, division, employee and date), "
                "employees and attendance, work done and overtime, blocks, divisions and estates"),
        starters=[
            "Total harvest in the last 30 days",
            "How many active employees per estate?",
            "Which blocks had the most ripe bunches this year?",
            "Harvest ripeness rate by division",
        ],
    )
    staging_engine = getattr(config, "staging_engine", None)
    staging = DataSource(
        id=STAGING,
        label="EPMS staging (all estates)",
        description="Central SQL Server staging database: EA, EB, EC, P1, P2.",
        dialect="mssql",
        schema="dbo",
        engine=staging_engine,
        scopes={"staging": {"label": "EPMS staging", "engine": staging_engine}},
        scope_order=["staging"],
        metadata_path=_ROOT / "schema_metadata_staging.json",
        rules_path=_ROOT / "business_rules_staging.txt",
        preamble_prompt="sql_system_preamble_staging",
        tables_collection="tables_staging",
        history_collection="query_history_staging",
        only_described=True,
        curated_ddl=True,
        freshness_sql="SELECT CONVERT(varchar(10), MAX(BUDAT), 23) FROM OPH WHERE WERKS IS NOT NULL",
        lint=_staging_lint,
        topics=("harvest tickets (ripe bunches and loose fruit by block, harvester and day), "
                "deliveries to the mill and their tonnage, attendance, daily field work and man-days, "
                "overtime, employees, and blocks with their area"),
        starters=[
            "Ripe bunches harvested per estate last month",
            "Monthly tonnes delivered to the mill per estate this year",
            "Daily harvest in Estate C over the last 30 days",
            "Who was absent (mangkir) per estate last week?",
        ],
        entities={
            code: {"werks": w, "label": label, "prfnr": prfnr, "pattern": _compile(aliases)}
            for code, (w, label, prfnr, aliases) in _STAGING_ENTITIES.items()
        },
    )
    return {LIVE: live, STAGING: staging}


def registry() -> dict[str, DataSource]:
    global _registry
    if _registry is None:
        _registry = _build()
    return _registry


def get(source_id: str | None) -> DataSource:
    src = registry().get(source_id or LIVE)
    if src is None:
        raise KeyError(f"Unknown data source {source_id!r}; expected one of {list(registry())}")
    return src


def available() -> list[DataSource]:
    return [s for s in registry().values() if s.available]


# ── the source in force for this request ───────────────────────────────────

_active: ContextVar[str] = ContextVar("data_source", default=LIVE)


def active() -> DataSource:
    return get(_active.get())


@contextmanager
def use(source_id: str | None):
    """Run the enclosed code against `source_id` (None = leave as it is)."""
    if source_id is None:
        yield active()
        return
    src = get(source_id)
    if not src.available:
        raise RuntimeError(f"Data source {source_id!r} is not configured")
    token = _active.set(source_id)
    try:
        yield src
    finally:
        _active.reset(token)
