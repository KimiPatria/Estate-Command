"""The few-shot example pool: where an example came from, which ones are shown,
and which queries may become one. Embeddings, Chroma and the database are stubbed."""
import concurrent.futures
from types import SimpleNamespace

import pytest

import chat_router
import query_history
import retrieval

OK_SQL = "SELECT SUM(bunches_total) FROM t_oph WHERE (oph_is_deleted = 0 OR oph_is_deleted IS NULL)"


class FakeHistory:
    def __init__(self, items):
        # items: (question, sql, distance, verified)
        self.items = items

    def count(self):
        return len(self.items)

    def query(self, query_embeddings, n_results, where):
        items = sorted(self.items, key=lambda i: i[2])[:n_results]
        return {"ids": [[str(n) for n in range(len(items))]],
                "documents": [[i[0] for i in items]],
                "metadatas": [[{"sql": i[1], "success": True, "verified": int(i[3])} for i in items]],
                "distances": [[i[2] for i in items]]}


@pytest.fixture
def pool(monkeypatch):
    def install(*items):
        monkeypatch.setattr(retrieval, "_index", lambda source_id=None: SimpleNamespace(
            history_collection=FakeHistory(list(items))))
        monkeypatch.setattr(retrieval, "_embed", lambda texts: [[0.0] for _ in texts])
        import sql_schema_check
        monkeypatch.setattr(sql_schema_check, "check",
                            lambda sql, d, s: "missing" if "ghost_col" in sql else None)
    return install


def _sql(n):
    return OK_SQL.replace("SUM(bunches_total)", f"SUM(bunches_total) AS v{n}")


def test_a_verified_example_wins_a_near_tie(pool):
    pool(("auto q", _sql(1), 0.10, False), ("seed q", _sql(2), 0.13, True))
    assert [q for q, _ in retrieval.retrieve_examples("x", k=1)] == ["seed q"]


def test_a_near_identical_unrated_question_beats_a_loose_seed(pool):
    pool(("auto q", _sql(1), 0.02, False), ("seed q", _sql(2), 0.25, True))
    assert [q for q, _ in retrieval.retrieve_examples("x", k=1)] == ["auto q"]


def test_unrelated_examples_are_left_out(pool):
    pool(("close", _sql(1), 0.20, False), ("far", _sql(2), 0.31, True))
    assert [q for q, _ in retrieval.retrieve_examples("x", k=3)] == ["close"]


def test_the_same_sql_is_shown_once(pool):
    pool(("a", _sql(1), 0.05, False), ("a again", _sql(1), 0.06, False), ("b", _sql(2), 0.07, False))
    assert [q for q, _ in retrieval.retrieve_examples("x", k=3)] == ["a", "b"]


def test_rule_breaking_or_stale_sql_is_never_shown(pool):
    no_guard = "SELECT SUM(bunches_total) FROM t_oph"
    pool(("lint", no_guard, 0.01, True), ("stale", _sql(1) + " AND ghost_col = 1", 0.02, True),
         ("good", _sql(2), 0.03, False))
    assert [q for q, _ in retrieval.retrieve_examples("x", k=3)] == ["good"]


def test_near_copies_of_the_question_can_be_excluded(pool):
    pool(("itself", _sql(1), 0.0, True), ("other", _sql(2), 0.15, False))
    assert [q for q, _ in retrieval.retrieve_examples("x", k=3, min_distance=0.10)] == ["other"]


def test_an_exact_match_is_kept_although_chroma_says_minus_zero(pool):
    # Chroma reports an exact match as -0.000; it once fell below the lower bound.
    pool(("the same question", _sql(1), -1e-7, False), ("other", _sql(2), 0.15, False))
    assert [q for q, _ in retrieval.retrieve_examples("x", k=1)] == ["the same question"]


def test_a_small_pool_is_still_used(pool):
    pool(("only one", _sql(1), 0.1, True))
    assert len(retrieval.retrieve_examples("x", k=3)) == 1


# ── provenance in query_history ────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(query_history, "_DB_PATH", tmp_path / "qh.db")
    monkeypatch.setattr(query_history, "_schema_ready", False)


def test_examples_carry_their_origin_and_verified_rank_first(db):
    a = query_history.record_query("auto", "SELECT 1", "bedrock", [], success=True, source="live")
    s = query_history.record_query("seed", "SELECT 2", "seed", [], success=True, source="live",
                                   origin="seed", verified=True, row_count=4)
    query_history.record_query("kept", "SELECT 3", "bedrock", [], success=False, source="live")
    rows = query_history.get_example_rows(source="live")
    assert [(r[0], r[3], r[4]) for r in rows] == [(s, 1, "seed"), (a, 0, "auto")]
    assert query_history.find_pair("seed", "SELECT 2", "live") == s
    assert query_history.find_pair("seed", "SELECT 2", "staging") is None


def test_a_thumbs_up_verifies_but_keeps_a_seed_a_seed(db):
    a = query_history.record_query("auto", "SELECT 1", "bedrock", [], success=True, source="live")
    s = query_history.record_query("seed", "SELECT 2", "seed", [], success=True, source="live",
                                   origin="seed", verified=True)
    query_history.set_verified(a)
    query_history.set_verified(s)
    assert query_history.get_example(a)[3:] == (1, "rated")
    assert query_history.get_example(s)[3:] == (1, "seed")


# ── only a sound query becomes an example ──────────────────────────────────

@pytest.fixture
def pipeline(monkeypatch):
    saved = []
    card = SimpleNamespace(name="t_oph")
    monkeypatch.setattr(chat_router, "retrieve_tables", lambda q, k=5: ([card], False))
    monkeypatch.setattr(chat_router, "retrieve_examples", lambda q, k=3: [])
    monkeypatch.setattr(chat_router, "_answer_llm",
                        lambda m, **kw: ("Answer.", "bedrock", {"input_tokens": 0, "output_tokens": 0, "model": "m"}))
    done = concurrent.futures.Future()
    done.set_result(None)
    monkeypatch.setattr(chat_router, "_start_chart", lambda *a: done)
    monkeypatch.setattr(chat_router, "record_query", lambda *a, **k: saved.append(k) or 1)

    def install(outcome, rows):
        res = chat_router.RunResult(sql="SELECT v FROM t", outcome=outcome, provider="bedrock",
                                    ok_estates=["ba", "k3"], run_step="run",
                                    exec_results={"ba": (["v"], rows, None), "k3": (["v"], rows, None)})
        monkeypatch.setattr(chat_router, "generate_and_run", lambda *a, **k: res)
        return saved
    return install


@pytest.mark.parametrize("outcome, rows, saved_as_example", [
    ("first_ok", [{"v": 3}], True),
    ("fixed_schema", [{"v": 3}], True),
    ("kept_empty", [], False),
    ("kept_suspicious", [{"v": -1}], False),
])
def test_only_sound_queries_become_examples(pipeline, outcome, rows, saved_as_example):
    saved = pipeline(outcome, rows)
    resp, _ = chat_router._run_sql_pipeline("s1", "total harvest in 2025", [], 0.0)
    assert resp.sql_outcome == outcome
    assert saved[0]["success"] is saved_as_example and saved[0]["row_count"] == 2 * len(rows)
