"""The pre-run schema check, the result checks and the correction loop
(chat_router.generate_and_run). Model, database and metadata are stubbed."""
from types import SimpleNamespace

import pytest

import chat_router
import data_sources
import metadata_loader
import sql_schema_check
from metadata_loader import ColumnSpec, TableCard


# ── the schema check ───────────────────────────────────────────────────────

def _card(name, *cols):
    return TableCard(name=name, description="", synonyms=[], example_questions=[],
                     columns=[ColumnSpec(c, "text", True, False) for c in cols])


@pytest.fixture
def cards(monkeypatch):
    monkeypatch.setitem(metadata_loader._CARDS, "pg", {
        "t_oph": _card("t_oph", "oph_id", "oph_block_code", "bunches_ripe", "oph_created_date", "oph_is_deleted"),
        "m_block": _card("m_block", "block_code", "block_hectarage", "block_state")})
    monkeypatch.setitem(metadata_loader._CARDS, "ms", {
        "OPH": _card("OPH", "WERKS", "BUDAT", "JJG"),
        "BLK": _card("BLK", "ESTNR", "BHA", "KDATE")})


@pytest.mark.parametrize("sql", [
    "SELECT SUM(bunches_ripe) AS v FROM t_oph WHERE (oph_is_deleted = 0 OR oph_is_deleted IS NULL)",
    "SELECT o.oph_block_code, SUM(o.bunches_ripe) AS total FROM public.t_oph o "
    "JOIN m_block b ON b.block_code = o.oph_block_code GROUP BY 1 ORDER BY total DESC LIMIT 3",
    "WITH x AS (SELECT oph_block_code AS blk, SUM(bunches_ripe) AS r FROM t_oph GROUP BY oph_block_code) "
    "SELECT blk, r FROM x ORDER BY r DESC",
    "SELECT DATE_TRUNC('month', oph_created_date) AS m, COUNT(*) FROM t_oph GROUP BY m ORDER BY m",
    "this is not sql at all",          # unparseable: left to the database
])
def test_real_names_pass_in_postgres(cards, sql):
    assert sql_schema_check.check(sql, "postgresql", "pg") is None


@pytest.mark.parametrize("sql", [
    "SELECT TOP 100 RIGHT(h.WERKS, 2) AS estate, SUM(h.JJG) * 1.0 / NULLIF(MAX(a.ha), 0) AS per_ha FROM OPH h "
    "JOIN (SELECT ESTNR, SUM(BHA) AS ha FROM BLK WHERE KDATE = '9999-12-31' GROUP BY ESTNR) a "
    "ON a.ESTNR = RIGHT(h.WERKS, 2) GROUP BY RIGHT(h.WERKS, 2)",
    "SELECT werks, COUNT(*) n FROM dbo.OPH GROUP BY werks HAVING COUNT(*) > 5 ORDER BY n DESC",
])
def test_real_names_pass_in_tsql(cards, sql):
    assert sql_schema_check.check(sql, "mssql", "ms") is None


def test_a_misspelt_column_is_named_with_the_closest_real_ones(cards):
    msg = sql_schema_check.check("SELECT SUM(bunches_rip) FROM t_oph", "postgresql", "pg")
    assert "bunches_rip does not exist in t_oph" in msg and "bunches_ripe" in msg


def test_a_tsql_column_keeps_its_case_in_the_message(cards):
    msg = sql_schema_check.check("SELECT [WERKS], SUM(JJGX) FROM OPH GROUP BY [WERKS]", "mssql", "ms")
    assert "jjgx" in msg.lower() and "JJG" in msg


def test_an_unknown_table_is_named(cards):
    msg = sql_schema_check.check("SELECT * FROM t_ophs", "postgresql", "pg")
    assert "table t_ophs does not exist" in msg and "t_oph" in msg


def test_without_metadata_nothing_is_checked():
    assert sql_schema_check.check("SELECT nope FROM nowhere", "postgresql", "no-such-source") is None


# ── result checks ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("cols, rows, flagged", [
    (["estate", "total_bunches"], [{"estate": "BA", "total_bunches": -5}], True),
    (["estate", "bunches_change"], [{"estate": "BA", "bunches_change": -5}], False),
    (["estate", "ripe_bunches_per_ha"], [{"estate": "EC", "ripe_bunches_per_ha": 1.11}], False),
    (["estate", "ripe_bunches_per_ha"], [{"estate": "EC", "ripe_bunches_per_ha": 0.42}], True),
    (["estate", "tonnes_per_ha"], [{"estate": "EC", "tonnes_per_ha": 0.42}], False),
    (["month", "total"], [{"month": "2025-01", "total": 10}], False),
])
def test_figures_no_query_gives_are_flagged(cols, rows, flagged):
    assert bool(data_sources.check_result(cols, rows)) is flagged


# ── the correction loop ────────────────────────────────────────────────────

GOOD = [{"estate": "BA", "total": 10}]


@pytest.fixture
def loop(monkeypatch):
    """Queue the SQL the writer returns and the rows each SQL gives."""
    state = SimpleNamespace(sql=[], llm=[], runs=[], rows={}, errors={}, schema_bad=set())

    def call_llm(messages, **kw):
        state.llm.append({"messages": messages, **kw})
        return (state.sql.pop(0) if state.sql else "SELECT 1"), "bedrock", {"input_tokens": 1, "output_tokens": 1}

    def execute(sql, estate_ids, max_rows=200):
        state.runs.append((sql, list(estate_ids)))
        out = {}
        for e in estate_ids:
            err = state.errors.get((sql, e))
            if isinstance(err, list):          # fail this many more times, then succeed
                err = err.pop(0) if err else None
            rows = state.rows.get(sql, GOOD)
            cols = list(rows[0]) if rows else ["estate", "total"]
            out[e] = ([], [], err) if err else (cols, rows, None)
        return out

    monkeypatch.setattr(chat_router, "call_llm", call_llm)
    monkeypatch.setattr(chat_router, "_execute_multi", execute)
    monkeypatch.setattr(chat_router, "build_sql_messages",
                        lambda q, cards, ex, prior_error=None, prior_sql=None, history_block=None:
                        [{"role": "user", "content": f"{q}|{prior_error or ''}|{prior_sql or ''}"}])
    monkeypatch.setattr(chat_router.sql_schema_check, "check",
                        lambda sql, d, s: "Wrong query: column x does not exist." if sql in state.schema_bad else None)
    monkeypatch.setattr(chat_router, "CHAT_SCHEMA_CHECK", "enforce")
    monkeypatch.setattr(chat_router, "CHAT_SQL_FIX_ATTEMPTS", 2)
    return state


def _go(state, question="total per estate", estates=("ba", "k3")):
    return chat_router.generate_and_run(question, question, [], [], None, list(estates))


def test_a_sound_query_runs_once(loop):
    loop.sql = ["SELECT a FROM t_x"]
    res = _go(loop)
    assert res.outcome == "first_ok" and len(loop.llm) == 1 and res.ok_estates == ["ba", "k3"]


def test_a_missing_column_is_corrected_before_anything_runs(loop):
    loop.sql = ["SELECT bad FROM t_x", "SELECT good FROM t_x"]
    loop.schema_bad = {"SELECT bad FROM t_x"}
    res = _go(loop)
    assert res.outcome == "fixed_schema" and res.sql == "SELECT good FROM t_x"
    assert [r[0] for r in loop.runs] == ["SELECT good FROM t_x"]          # the bad one never ran
    assert loop.llm[1]["tier"] == "reasoning" and "does not exist" in loop.llm[1]["messages"][0]["content"]


def test_an_estate_that_failed_alone_is_run_again_as_it_is(loop):
    loop.sql = ["SELECT a FROM t_x"]
    loop.errors[("SELECT a FROM t_x", "ba")] = ["connection reset"]
    res = _go(loop)
    assert res.outcome == "first_ok" and res.ok_estates == ["ba", "k3"] and len(loop.llm) == 1
    assert loop.runs == [("SELECT a FROM t_x", ["ba", "k3"]), ("SELECT a FROM t_x", ["ba"])]


def test_an_empty_result_gets_one_look_and_is_kept_if_unchanged(loop):
    loop.sql = ["SELECT a FROM t_x", "SELECT a FROM t_x"]
    loop.rows["SELECT a FROM t_x"] = []
    res = _go(loop)
    assert res.outcome == "kept_empty" and len(loop.llm) == 2 and res.ran


def test_an_empty_result_is_replaced_by_one_with_rows(loop):
    loop.sql = ["SELECT a FROM t_x WHERE x = 'ec'", "SELECT a FROM t_x WHERE x = 'EC'"]
    loop.rows["SELECT a FROM t_x WHERE x = 'ec'"] = []
    res = _go(loop)
    assert res.outcome == "fixed_empty" and res.sql.endswith("'EC'")


def test_an_empty_result_about_yesterday_is_not_second_guessed(loop):
    loop.sql = ["SELECT a FROM t_x"]
    loop.rows["SELECT a FROM t_x"] = []
    res = _go(loop, question="total harvest yesterday")
    assert res.outcome == "kept_empty" and len(loop.llm) == 1


def test_an_all_zero_total_counts_as_empty(loop):
    loop.sql = ["SELECT a FROM t_x", "SELECT a FROM t_x"]
    loop.rows["SELECT a FROM t_x"] = [{"estate": "BA", "total": 0}]
    assert _go(loop, estates=("ba",)).outcome == "kept_empty"


def test_a_zero_total_on_every_estate_counts_as_empty(loop):
    loop.sql = ["SELECT n FROM t_deleted", "SELECT n FROM t_x"]
    loop.rows["SELECT n FROM t_deleted"] = [{"n": 0}]
    loop.rows["SELECT n FROM t_x"] = [{"n": 66}]
    res = _go(loop)                     # two estates, one zero row each
    assert res.outcome == "fixed_empty" and res.sql == "SELECT n FROM t_x"


def test_an_implausible_result_is_replaced_and_kept_only_as_a_fallback(loop):
    loop.sql = ["SELECT a FROM t_x", "SELECT b FROM t_x", "SELECT c FROM t_x"]
    for s in loop.sql:
        loop.rows[s] = [{"estate": "EC", "ripe_bunches_per_ha": 0.3}]
    res = _go(loop, estates=("ba",))
    assert res.outcome == "kept_suspicious" and res.sql == "SELECT a FROM t_x"   # the first stands
    assert len(loop.llm) == 3 and res.notes and "may be wrong" in res.notes[0]


def test_the_corrections_stop_at_the_budget(loop):
    loop.sql = ["SELECT a FROM t_x", "SELECT b FROM t_x", "SELECT c FROM t_x", "SELECT d FROM t_x"]
    for s in loop.sql:
        for e in ("ba", "k3"):
            loop.errors[(s, e)] = "syntax error"
    res = _go(loop)
    assert res.outcome == "failed" and len(loop.llm) == 3 and not res.ran


def test_a_query_still_breaking_a_rule_is_never_run(loop):
    loop.sql = ["SELECT a FROM t_x", "SELECT b FROM t_x", "SELECT c FROM t_x"]
    loop.schema_bad = set(loop.sql)
    res = _go(loop)
    assert res.outcome == "failed" and loop.runs == []


def test_a_correction_that_changes_nothing_ends_the_loop(loop):
    loop.sql = ["SELECT a FROM t_x", "SELECT a FROM t_x"]
    for e in ("ba", "k3"):
        loop.errors[("SELECT a FROM t_x", e)] = "permission denied"
    res = _go(loop)
    assert res.outcome == "failed" and len(loop.llm) == 2


def test_unsafe_sql_is_refused_at_once(loop):
    loop.sql = ["DELETE FROM t_x"]
    res = _go(loop)
    assert res.outcome == "unsafe" and loop.runs == [] and len(loop.llm) == 1
