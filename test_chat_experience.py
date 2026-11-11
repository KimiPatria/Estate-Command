"""The chat experience around an answer: charts, live steps, exact comparisons,
the tonnage double-count check, and chat housekeeping. No LLM, no database."""

import threading

import pytest

import chat_charts as cc
import chat_conversation as conv
import chat_events
import chat_history
import chat_router
import data_sources
import query_history


# ── charts ─────────────────────────────────────────────────────────────────

ESTATES = [
    {"estate_name": "ESTATE A", "ripe_bunches_last_month": 507293.0},
    {"estate_name": "ESTATE B", "ripe_bunches_last_month": 792286.0},
    {"estate_name": "PLASMA 1", "ripe_bunches_last_month": 58042.0},
]
MONTHLY = [
    {"estate": "EA", "year": 2026, "month": m, "tonnes": 100.0 * m} for m in (1, 2, 3)
] + [
    {"estate": "EB", "year": 2026, "month": m, "tonnes": 50.0 * m} for m in (1, 2, 3)
]


def chart(question, rows):
    return cc.plan_chart(question, list(rows[0]), rows, use_llm=False)


def test_a_measure_named_after_a_period_is_still_a_measure():
    _, _, kinds = cc.profile(list(ESTATES[0]), ESTATES)
    assert kinds == {"estate_name": "category", "ripe_bunches_last_month": "number"}


def test_categories_become_bars_with_the_database_numbers():
    c = chart("Ripe bunches per estate last month", ESTATES)
    assert c["type"] == "bar"
    assert c["labels"] == ["ESTATE A", "ESTATE B", "PLASMA 1"]
    assert c["series"][0]["values"] == [507293.0, 792286.0, 58042.0]


def test_year_and_month_fold_into_one_time_axis_with_a_line_per_estate():
    c = chart("Monthly tonnes per estate", MONTHLY)
    assert c["type"] == "line"
    assert c["labels"] == ["2026-01", "2026-02", "2026-03"]
    assert {s["name"]: s["values"] for s in c["series"]} == {
        "EA": [100.0, 200.0, 300.0], "EB": [50.0, 100.0, 150.0]}


def test_a_named_chart_type_is_honoured():
    assert chart("show that as a pie chart", ESTATES)["type"] == "pie"
    assert chart("plot it as a line chart", MONTHLY)["type"] == "line"


def test_a_pie_only_when_asked_for():
    spec = cc.validate({"chart": "pie", "x": "estate_name", "y": ["ripe_bunches_last_month"]},
                       *_profile(ESTATES))
    assert spec["type"] == "pie"                       # the model may suggest it ...
    assert chart("Ripe bunches per estate", ESTATES)["type"] != "pie"   # ... the rule never does


def _profile(rows):
    cols, _, kinds = cc.profile(list(rows[0]), rows)
    return cols, kinds


@pytest.mark.parametrize("raw", [
    {"chart": "bar", "x": "nope", "y": ["ripe_bunches_last_month"]},      # invented column
    {"chart": "bar", "x": "estate_name", "y": ["estate_name"]},           # a name is not a measure
    {"chart": "radar", "x": "estate_name", "y": ["ripe_bunches_last_month"]},
    {"chart": "line", "x": "estate_name"},                                # nothing to plot
    "bar",
])
def test_a_bad_chart_spec_is_refused(raw):
    assert cc.validate(raw, *_profile(ESTATES)) is None


def test_measures_of_different_size_do_not_share_an_axis():
    rows = [{"block": "A1", "bunches": 5000.0, "area_ha": 12.5},
            {"block": "A2", "bunches": 7000.0, "area_ha": 14.0}]
    c = chart("bunches and area per block", rows)
    assert [s["name"] for s in c["series"]] == ["Bunches"]


def test_nothing_to_chart():
    assert chart("who is the manager", [{"name": "A"}, {"name": "B"}]) is None
    assert chart("total", [{"total": 5.0}]) is None


def test_rankings_are_sorted_and_long_lists_are_cut():
    rows = [{"block": f"B{i:03d}", "bunches": float(i)} for i in range(1, 51)]
    c = chart("bunches per block", rows)
    assert c["type"] == "hbar" and c["truncated"]
    assert c["labels"][0] == "B050" and len(c["labels"]) == cc.MAX_BAR_LABELS


def test_chart_requests_are_recognised():
    assert cc.wants_chart("can you plot that") and cc.wants_chart("tampilkan grafik")
    assert not cc.wants_chart("total harvest last month")
    assert cc.requested_type("as a donut") is None            # no chart word, no type
    assert cc.requested_type("as a donut chart") == "pie"


# ── "plot that" and small talk ─────────────────────────────────────────────

HISTORY = [{"rows": [{"a": 1}]}]


@pytest.mark.parametrize("message,expected", [
    ("plot that", True),
    ("show it as a line chart", True),
    ("as a pie chart", True),
    ("chart the harvest per month", False),      # names data: a new question
    ("what about last month?", False),
])
def test_chart_follow_up_reuses_the_rows_on_screen(message, expected):
    assert conv.chart_followup(message, HISTORY) is expected


def test_no_rows_no_chart_follow_up():
    assert not conv.chart_followup("plot that", [{"rows": []}])


@pytest.mark.parametrize("message", ["hi", "Hello!", "thanks a lot", "terima kasih", "what can you do?", "help"])
def test_small_talk(message):
    assert conv.is_smalltalk(message)


@pytest.mark.parametrize("message", ["hi, total harvest last month?", "total panen", "help me compare EA and EB"])
def test_a_question_is_not_small_talk(message):
    assert not conv.is_smalltalk(message)


def test_small_talk_does_not_use_up_the_pipeline_history():
    turns = [{"route": "sql", "question": "q", "meta": {}}, {"route": "chat", "question": "thanks", "meta": {}}]
    assert [t["question"] for t in conv.pipeline_turns(turns)] == ["q"]


# ── live steps ─────────────────────────────────────────────────────────────

def test_helpers_do_nothing_without_a_recorder():
    chat_events.step("x", "X"); chat_events.done("x"); chat_events.send("delta", text="a")
    assert not chat_events.streaming()


def test_steps_are_sent_and_kept():
    seen = []
    with chat_events.recording(lambda e, d: seen.append((e, d["status"]))) as rec:
        assert chat_events.streaming()
        chat_events.step("sql", "Writing the query")
        chat_events.done("sql", sql="SELECT 1")
        chat_events.step("run", "Running")
        chat_events.done("run", status="error", detail="rejected")
    assert seen == [("step", "running"), ("step", "done"), ("step", "running"), ("step", "error")]
    summary = rec.summary()
    assert [(s["id"], s["status"]) for s in summary] == [("sql", "done"), ("run", "error")]
    assert summary[0]["sql"] == "SELECT 1" and "ms" in summary[0] and "_t0" not in summary[0]


def test_stopping_unwinds_past_ordinary_error_handling():
    cancel = threading.Event()
    with chat_events.recording(lambda e, d: None, cancel):
        chat_events.step("a", "A")
        cancel.set()
        with pytest.raises(chat_events.Cancelled):
            try:
                chat_events.step("b", "B")
            except Exception:          # what the pipeline's own handlers catch
                pytest.fail("a stop must not be swallowed as an ordinary error")


# ── exact comparisons for the answer ───────────────────────────────────────

def test_derived_facts_are_exact():
    d = chat_router._derived_facts(["estate_name", "ripe_bunches_last_month"], ESTATES)["ripe_bunches_last_month"]
    assert d["highest"] == {"which": "ESTATE B", "value": 792286.0}
    assert d["lowest"] == {"which": "PLASMA 1", "value": 58042.0}
    assert d["highest_minus_lowest"] == 734244.0
    assert d["highest_is_x_times_lowest"] == 13.7
    assert d["share_of_sum_pct"]["ESTATE B"] == 58.4


def test_derived_facts_skip_labels_and_single_rows():
    assert chat_router._derived_facts(["year", "v"], [{"year": 2026, "v": 1.0}]) == {}
    d = chat_router._derived_facts(["year", "v"], [{"year": 2025, "v": 1.0}, {"year": 2026, "v": 3.0}])
    assert list(d) == ["v"] and d["v"]["first_row_to_last_row_change_pct"] == 200.0


# ── tonnage must not be double counted ─────────────────────────────────────

ONCE_PER_TRIP = ("SELECT e.NAME1, SUM(t.w) / 1000.0 FROM (SELECT ESTNR, SPB, MIN(BUDAT) AS d, "
                 "MAX(WB_QTY_TOTAL) AS w FROM FDN GROUP BY ESTNR, SPB) t "
                 "JOIN EST e ON e.ESTNR = t.ESTNR GROUP BY e.NAME1")


def test_weight_taken_once_per_trip_passes():
    lint = data_sources.get("staging").lint
    assert lint(ONCE_PER_TRIP) is None
    assert lint("SELECT WERKS, SUM(ZMASAK) FROM OPH GROUP BY WERKS") is None


@pytest.mark.parametrize("sql", [
    "SELECT ESTNR, SUM(WB_QTY_TOTAL) FROM FDN GROUP BY ESTNR",
    "SELECT AVG(f.WB_QTY_TOTAL) FROM FDN f",
    # the per-trip subquery joined back to the delivery rows
    "SELECT MONTH(f.BUDAT), SUM(t.w) FROM (SELECT SPB, MAX(WB_QTY_TOTAL) AS w FROM FDN GROUP BY SPB) t "
    "JOIN FDN f ON f.SPB = t.SPB GROUP BY MONTH(f.BUDAT)",
    "SELECT SUM(t.w) FROM (SELECT SPB, OPH, MAX(WB_QTY_TOTAL) AS w FROM FDN GROUP BY SPB, OPH) t "
    "JOIN OPH o ON o.OPH = t.OPH",
])
def test_double_counted_weight_is_flagged(sql):
    assert "Wrong result" in data_sources.get("staging").lint(sql)


# ── live: harvest date (rule 5) and deleted rows (rule 3) ──────────────────

GOOD_LIVE = ("SELECT SUM(bunches_total) FROM t_oph WHERE EXTRACT(YEAR FROM oph_created_date) = 2025 "
             "AND (oph_is_deleted = 0 OR oph_is_deleted IS NULL)")


def test_live_sql_that_follows_the_rules_passes():
    lint = data_sources.get("live").lint
    assert lint(GOOD_LIVE, "Total harvest in 2025") is None
    assert lint("SELECT COUNT(*) FROM m_employee", "how many employees") is None     # no flag column
    assert lint("SELECT * FROM t_oph_persons", "who harvested") is None               # a different table


@pytest.mark.parametrize("sql", [
    GOOD_LIVE.replace("oph_created_date", "oph_approved_date"),
    "SELECT SUM(bunches_total) FROM t_oph o JOIN m_block b ON b.block_code = o.oph_block_code "
    "WHERE o.oph_approved_date >= '2025-01-01' AND o.oph_is_deleted = 0",
])
def test_the_approval_date_is_not_the_harvest_date(sql):
    assert "oph_created_date" in data_sources.get("live").lint(sql, "Total harvest in 2025")


def test_approval_date_is_fine_when_approval_is_asked_about():
    sql = "SELECT COUNT(*) FROM t_oph WHERE oph_approved_date >= '2025-01-01' AND oph_is_deleted = 0"
    assert data_sources.get("live").lint(sql, "How many tickets were approved in 2025?") is None


@pytest.mark.parametrize("table,flag", [("t_oph", "oph_is_deleted"), ("t_fdn", "fdn_is_deleted"), ("t_cp", "cp_is_deleted")])
def test_deleted_rows_must_be_excluded(table, flag):
    lint = data_sources.get("live").lint
    assert flag in lint(f"SELECT COUNT(*) FROM {table}", "how many records")
    assert lint(f"SELECT COUNT(*) FROM {table} WHERE ({flag} = 0 OR {flag} IS NULL)", "how many records") is None


def test_asking_about_deleted_rows_is_not_blocked():
    lint = data_sources.get("live").lint
    assert lint("SELECT COUNT(*) FROM t_oph WHERE oph_is_deleted = 1", "How many harvest records were deleted?") is None
    assert lint("SELECT COUNT(*) FROM t_oph", "berapa data panen yang dihapus") is None


# ── chats: rename, pin, regenerate, feedback ───────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(query_history, "_DB_PATH", tmp_path / "chat.db")
    monkeypatch.setattr(query_history, "_schema_ready", False)
    chat_history.init_chat_db()
    return chat_history


def test_rename_and_pin(store):
    sid = store.ensure_session(None, "first question")
    store.add_turn(sid, "first question", "answer")
    other = store.ensure_session(None, "second")
    store.add_turn(other, "second", "answer")
    assert store.update_session(sid, title="  Harvest   review ", pinned=True)
    first = store.list_sessions()[0]
    assert (first["id"], first["title"], first["pinned"]) == (sid, "Harvest review", 1)
    assert not store.update_session(sid)                    # nothing to change
    assert not store.update_session("missing", title="x")


def test_regenerate_drops_only_the_newest_turn(store):
    sid = store.ensure_session(None, "q1")
    store.add_turn(sid, "q1", "a1")
    store.add_turn(sid, "q2", "a2")
    assert store.delete_last_turn(sid)
    assert [t["question"] for t in store.get_turns(sid)] == ["q1"]
    assert store.delete_last_turn(sid) and not store.delete_last_turn(sid)


def test_feedback_is_stored_with_the_turn(store):
    sid = store.ensure_session(None, "q")
    turn = store.add_turn(sid, "q", "a", meta={"query_id": 7})
    assert store.set_feedback(sid, turn, "down") == {"query_id": 7}
    assert store.get_turns(sid)[0]["feedback"] == "down"
    assert store.set_feedback("another-chat", turn, "up") is None


def test_thumbs_down_retires_the_query_as_an_example(store):
    qid = query_history.record_query("q", "SELECT 1", "p", ["t"], True, source="staging")
    query_history.set_success(qid, False)
    assert query_history.get_successful_pairs(source="staging") == []



# ── block area summed over transaction rows ────────────────────────────────

FANOUT_STAGING = """WITH estate_data AS (
    SELECT e.WERKS, e.NAME1, SUM(CAST(o.ZMASAK AS decimal(18,2))) AS bunches, SUM(b.BHA) AS hectares
    FROM OPH o JOIN EST e ON e.WERKS = o.WERKS
    JOIN BLK b ON b.ESTNR = RIGHT(o.WERKS, 2) AND b.BLOCK = o.BLOCK AND b.KDATE = '9999-12-31'
    WHERE o.BUDAT >= '2025-01-01' GROUP BY e.WERKS, e.NAME1)
SELECT TOP 100 WERKS, bunches / hectares AS per_ha FROM estate_data"""

SEPARATE_STAGING = """SELECT e.NAME1 AS estate, h.b / NULLIF(a.ha, 0) AS per_ha
FROM (SELECT WERKS, SUM(CAST(ZMASAK AS decimal(18,2))) AS b FROM OPH WHERE WERKS IS NOT NULL GROUP BY WERKS) h
JOIN (SELECT ESTNR, SUM(BHA) AS ha FROM BLK WHERE KDATE = '9999-12-31' GROUP BY ESTNR) a ON a.ESTNR = RIGHT(h.WERKS, 2)
JOIN EST e ON e.WERKS = h.WERKS AND e.KDATE = '9999-12-31'"""


def test_area_summed_over_harvest_tickets_is_flagged():
    msg = data_sources.get("staging").lint(FANOUT_STAGING, "bunches per hectare")
    assert msg and "OPH" in msg and "SUM(BHA)" in msg


@pytest.mark.parametrize("sql", [
    SEPARATE_STAGING,
    "SELECT ESTNR, SUM(BHA) AS ha FROM BLK WHERE KDATE = '9999-12-31' AND BSTATE = 'MATURE' GROUP BY ESTNR",
    # block area of the blocks harvested, taken once per block first
    "SELECT SUM(x.BHA) FROM (SELECT DISTINCT b.ESTNR, b.BLOCK, b.BHA FROM OPH o JOIN BLK b "
    "ON b.ESTNR = RIGHT(o.WERKS, 2) AND b.BLOCK = o.BLOCK AND b.KDATE = '9999-12-31') x",
])
def test_area_summed_on_its_own_passes(sql):
    assert data_sources.get("staging").lint(sql, "bunches per hectare") is None


def test_area_check_on_live():
    lint = data_sources.get("live").lint
    bad = ("SELECT SUM(o.bunches_total) / SUM(b.block_hectarage) FROM t_oph o JOIN m_block b "
           "ON b.block_code = o.oph_block_code WHERE (o.oph_is_deleted = 0 OR o.oph_is_deleted IS NULL)")
    good = ("SELECT h.b / NULLIF(a.ha, 0) FROM (SELECT SUM(bunches_total) AS b FROM t_oph "
            "WHERE (oph_is_deleted = 0 OR oph_is_deleted IS NULL)) h CROSS JOIN "
            "(SELECT SUM(block_hectarage) AS ha FROM m_block WHERE block_state = 'MATURE') a")
    assert "t_oph" in lint(bad, "bunches per hectare")
    assert lint(good, "bunches per hectare") is None


# ── the period, when the question names none ───────────────────────────────

def test_period_note_resolves_the_default_window():
    from datetime import date
    sql = ("SELECT 1 FROM OPH WHERE BUDAT >= DATEADD(month, -12, DATEFROMPARTS(YEAR(GETDATE()), "
           "MONTH(GETDATE()), 1)) AND BUDAT < DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)")
    note = chat_router._period_note(sql, today=date(2026, 11, 10))
    assert "1 Nov 2025 to 31 Oct 2026" in note


def test_period_note_otherwise_points_at_the_sql():
    from datetime import date
    note = chat_router._period_note("SELECT 1 FROM OPH WHERE BUDAT >= '2025-01-01'", today=date(2026, 11, 10))
    assert "2026-11-10" in note and "last 12 full months" not in note


def test_period_note_reaches_the_answer_model():
    msgs = chat_router._build_answer_messages("q", "SELECT 1", [], [], {}, period_note="NOTE-X")
    assert "Period: NOTE-X" in msgs[1]["content"]
    assert "Period:" not in chat_router._build_answer_messages("q", "SELECT 1", [], [], {})[1]["content"]



@pytest.mark.parametrize("sql", [
    "SELECT 1 FROM EST e JOIN h ON RIGHT(e.WERKS, 2) = h.WERKS",
    "SELECT 1 FROM h JOIN a ON a.ESTNR = h.WERKS",
    "SELECT 1 FROM h JOIN EST e ON h.WERKS = RIGHT(e.WERKS, 2)",
])
def test_werks_compared_with_a_two_letter_code_is_flagged(sql):
    assert "matches no row" in data_sources.get("staging").lint(sql, "q")


@pytest.mark.parametrize("sql", [
    "SELECT 1 FROM OPH o JOIN EST e ON e.WERKS = o.WERKS",
    "SELECT 1 FROM OPH o JOIN BLK b ON b.ESTNR = RIGHT(o.WERKS, 2) AND b.BLOCK = o.BLOCK",
    "SELECT 1 FROM h JOIN a ON RIGHT(h.WERKS, 2) = a.ESTNR",
    "SELECT 1 FROM OPH WHERE WERKS = '00EC'",
])
def test_matching_estate_keys_pass(sql):
    assert data_sources.get("staging").lint(sql, "q") is None



DEFAULT_SQL = ("SELECT 1 FROM OPH WHERE BUDAT >= DATEADD(month, -12, DATEFROMPARTS(YEAR(GETDATE()), "
               "MONTH(GETDATE()), 1)) AND BUDAT < DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)")


def test_default_period_is_added_when_the_answer_leaves_it_out(monkeypatch):
    out = chat_router._with_period("**Estate C leads.**", "bunches per hectare by estate", DEFAULT_SQL)
    assert out.startswith("**Estate C leads.**") and "_Covers the last 12 full months: " in out


@pytest.mark.parametrize("answer,question,sql", [
    ("**Estate C leads over the last 12 months.**", "bunches per hectare", DEFAULT_SQL),   # already said
    ("**Estate C leads.**", "bunches per hectare in 2025", DEFAULT_SQL),                   # period asked
    ("**Estate C leads.**", "bunches per hectare", "SELECT 1 FROM OPH WHERE BUDAT >= '2025-01-01'"),
])
def test_period_line_only_when_needed(answer, question, sql):
    assert chat_router._with_period(answer, question, sql) == answer



def test_estate_name_joined_to_prfnr_is_flagged():
    lint = data_sources.get("staging").lint
    assert "plasma" in lint("SELECT e.NAME1, COUNT(*) FROM ATD a JOIN EST e ON e.NAME1 = a.PRFNR GROUP BY e.NAME1", "q")
    assert lint("SELECT PRFNR, COUNT(DISTINCT EMPNR) FROM ATD WHERE ATDCD = 'M' GROUP BY PRFNR", "q") is None
