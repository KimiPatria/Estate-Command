"""Switching the chat between the live Postgres databases and the SQL Server
staging database: the registry, the T-SQL guardrails, estate scoping, and the
per-source query history. No LLM and (except where marked) no database needed."""

import threading

import pytest

import data_sources
import query_history
from sql_validator import ensure_limit, validate


# ── the registry and the active source ─────────────────────────────────────

def test_live_is_the_default():
    assert data_sources.active().id == data_sources.LIVE


def test_use_switches_and_restores():
    if not data_sources.get("staging").available:
        pytest.skip("DATABASE_URL_STAGING not configured")
    with data_sources.use("staging"):
        assert data_sources.active().id == "staging"
        assert data_sources.active().dialect == "mssql"
    assert data_sources.active().id == data_sources.LIVE


def test_use_none_leaves_the_source_alone():
    with data_sources.use(None) as src:
        assert src.id == data_sources.LIVE


def test_unknown_source_is_an_error():
    with pytest.raises(KeyError):
        data_sources.get("nope")


def test_unconfigured_source_cannot_be_activated(monkeypatch):
    monkeypatch.setattr(data_sources.get("staging"), "engine", None)
    with pytest.raises(RuntimeError):
        with data_sources.use("staging"):
            pass


def test_active_source_is_per_thread():
    """Two requests on different databases must not see each other: the source
    lives in a ContextVar, and a new thread starts from the default."""
    seen = []
    with data_sources.use(data_sources.LIVE):
        t = threading.Thread(target=lambda: seen.append(data_sources.active().id))
        t.start()
        t.join()
    assert seen == [data_sources.LIVE]


# ── estate scoping on staging ──────────────────────────────────────────────

@pytest.fixture
def staging():
    return data_sources.get("staging")


@pytest.mark.parametrize("text,codes", [
    ("ripe bunches in estate EC last month", ["EC"]),
    ("compare Estate A and Estate B", ["EA", "EB"]),
    ("how is plasma 1 doing", ["P1"]),
    ("kebun c panen kemarin", ["EC"]),
    ("total harvest last month", []),
    ("check the ecology report", []),      # "ec" inside a word is not an estate
])
def test_detect_entities(staging, text, codes):
    assert staging.detect_entities(text) == codes


def test_scope_hint_covers_every_table_shape(staging):
    hint = staging.scope_hint(["EB", "P1"])
    assert "WERKS IN ('00EB', '00P1')" in hint       # OPH, GWO, OVT
    assert "ESTNR IN ('EB', 'P1')" in hint           # FDN, BLK, DIV, EST
    assert "PRFNR IN ('ESTATE B', 'ESTATE PLASMA 1')" in hint   # ATD, EMP, MEM


def test_scope_hint_is_none_for_all_estates(staging):
    assert staging.scope_hint([]) is None
    assert staging.scope_hint(["XX"]) is None


# ── T-SQL guardrails ───────────────────────────────────────────────────────

@pytest.mark.parametrize("sql", [
    "SELECT TOP 10 WERKS, SUM(ZMASAK) FROM OPH GROUP BY WERKS ORDER BY 2 DESC",
    "WITH x AS (SELECT BLOCK FROM BLK) SELECT * FROM x",
    "SELECT [DESC] FROM ATC",
    "SELECT a.BULKY, a.system_x FROM x a",                       # not the keywords
    "SELECT COUNT(*) FROM OPH WHERE BUDAT >= DATEADD(day, -30, CAST(GETDATE() AS date))",
])
def test_mssql_allows_reads(sql):
    assert validate(sql, "mssql") == (True, None)


@pytest.mark.parametrize("sql", [
    "SELECT * INTO #t FROM OPH",
    "SELECT * FROM sys.sql_logins",
    "SELECT * FROM master.dbo.x",
    "EXEC sp_who",
    "EXEC xp_cmdshell 'dir'",
    "SELECT * FROM OPH; DROP TABLE OPH",
    "UPDATE EST SET NAME1 = 'x'",
    "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'y')",
])
def test_mssql_blocks_writes_and_escape_hatches(sql):
    ok, reason = validate(sql, "mssql")
    assert not ok and reason


def test_postgres_validation_is_unchanged():
    assert validate("SELECT * FROM t_oph LIMIT 5") == (True, None)
    # sys. and INTO are only special on SQL Server
    assert validate("SELECT sys.x FROM sys") == (True, None)


def test_limit_is_appended_on_postgres_only():
    assert ensure_limit("SELECT 1 FROM x;", 10) == "SELECT 1 FROM x LIMIT 10"
    assert ensure_limit("SELECT 1 FROM x LIMIT 3", 10) == "SELECT 1 FROM x LIMIT 3"
    # T-SQL has no trailing LIMIT; the cap is applied where rows are fetched
    assert ensure_limit("SELECT TOP 5 1 FROM x;", 10, "mssql") == "SELECT TOP 5 1 FROM x"


# ── query history keeps each database's examples apart ─────────────────────

@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(query_history, "_DB_PATH", tmp_path / "qh.db")
    monkeypatch.setattr(query_history, "_schema_ready", False)
    return query_history


def test_history_is_per_source(history):
    history.record_query("pg question", "SELECT 1 LIMIT 1", "p", ["t_oph"], True)
    history.record_query("tsql question", "SELECT TOP 1 1", "p", ["OPH"], True, source="staging")
    assert [q for _, q, _ in history.get_successful_pairs(source="live")] == ["pg question"]
    assert [q for _, q, _ in history.get_successful_pairs(source="staging")] == ["tsql question"]


def test_history_migrates_an_old_database(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:      # the table as it was before `source` existed
        conn.execute("""CREATE TABLE queries (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL,
                        sql TEXT NOT NULL, provider TEXT NOT NULL, tables_used TEXT, success INTEGER NOT NULL,
                        error TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("INSERT INTO queries (question, sql, provider, success) VALUES ('old', 'SELECT 1', 'p', 1)")
    monkeypatch.setattr(query_history, "_DB_PATH", path)
    monkeypatch.setattr(query_history, "_schema_ready", False)
    assert [q for _, q, _ in query_history.get_successful_pairs(source="live")] == ["old"]
    assert query_history.get_successful_pairs(source="staging") == []
