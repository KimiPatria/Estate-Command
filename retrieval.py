import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

import data_sources
import metadata_loader
from dataclasses import dataclass, field
from metadata_loader import TableCard, embedding_text, load_all_cards
from query_history import get_example, get_example_rows, get_successful_pairs
from llm import call_routing_llm
from error_handler import is_off_domain as _is_off_domain

log = logging.getLogger(__name__)

_EMBED_MODEL   = "BAAI/bge-small-en-v1.5"
_CHROMA_PATH   = "./chroma_store"
_TABLE_COLL    = "tables"
_HISTORY_COLL  = "query_history"

# Module-level singletons — set by initialize()
_embedder:          Any = None
_chroma_client:     Any = None


@dataclass
class _Index:
    """Retrieval state of one data source. Each source has its own table
    collection, BM25 index and query-history collection: the staging database
    shares no tables, SQL dialect or past queries with the live one."""
    table_collection:   Any = None
    history_collection: Any = None
    bm25_index:         Any = None   # rank_bm25.BM25Okapi
    bm25_table_names:   list[str] = field(default_factory=list)


_INDEXES: dict[str, _Index] = {}


def _index(source_id: str | None = None) -> _Index:
    return _INDEXES.get(source_id or data_sources.active().id) or _Index()


# ── tokenizer ──────────────────────────────────────────────────────────────

# Table prefix letters and common noise tokens that appear in every document
_STOPWORDS = {"t", "m", "tr", "mc", "id", "by", "the", "a", "of", "is", "in", "and", "or"}

# Tables whose names start with these prefixes are approval/audit logs — penalise them
_AUDIT_PREFIXES = ("log_", "audit_", "login_")


def _tokenize(text: str) -> list[str]:
    """Split text for BM25: handles underscores, hyphens, camelCase, and stopwords."""
    # Split camelCase boundaries
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    # Replace underscores, hyphens, dots with spaces
    text = re.sub(r'[_\-\.]', ' ', text)
    tokens = text.lower().split()
    return [tok for tok in tokens if tok not in _STOPWORDS and len(tok) > 1]


# ── embedding ──────────────────────────────────────────────────────────────

def _embed(texts: list[str]) -> list[list[float]]:
    global _embedder
    if _embedder is None:
        try:
            from fastembed import TextEmbedding
            _embedder = TextEmbedding(model_name=_EMBED_MODEL)
            log.info("fastembed model '%s' loaded", _EMBED_MODEL)
        except Exception as e:
            raise RuntimeError(
                f"fastembed model download/init failed: {e}. "
                "Check network access and that 'fastembed' is installed."
            ) from e
    embeddings = list(_embedder.embed(texts))
    return [e.tolist() for e in embeddings]


# ── ChromaDB ───────────────────────────────────────────────────────────────

def _init_chroma() -> Any:
    global _chroma_client
    import chromadb

    def _make_client() -> Any:
        return chromadb.PersistentClient(path=_CHROMA_PATH)

    try:
        client = _make_client()
        client.list_collections()  # smoke-test
        _chroma_client = client
        return client
    except Exception as e:
        log.warning("ChromaDB init failed (%s) — resetting chroma_store/ and retrying.", e)
        p = Path(_CHROMA_PATH)
        if p.exists():
            shutil.rmtree(p)
        client = _make_client()
        _chroma_client = client
        return client


# ── public initializer ─────────────────────────────────────────────────────

def initialize(cards: dict[str, TableCard] | None = None, source: str | None = None) -> None:
    """Build dense + BM25 indexes for one data source (default: live). Called at server startup."""
    from rank_bm25 import BM25Okapi

    src = data_sources.get(source)
    if cards is None:
        cards = metadata_loader.cards(src.id) or load_all_cards(source=src)

    log.info("[%s] initializing retrieval indexes for %d tables...", src.id, len(cards))
    t0 = time.monotonic()

    client = _init_chroma()

    # ── table dense index ────────────────────────────────────────────────
    table_names = sorted(cards.keys())
    texts = [embedding_text(cards[name]) for name in table_names]

    embeddings = _embed(texts)   # raises clearly on network failure

    idx = _Index()
    idx.table_collection = client.get_or_create_collection(
        name=src.tables_collection,
        metadata={"hnsw:space": "cosine"},
    )
    # A stale entry (a table since hidden or dropped) must not survive a re-index.
    try:
        stale = set(idx.table_collection.get()["ids"]) - set(table_names)
        if stale:
            idx.table_collection.delete(ids=sorted(stale))
    except Exception as e:
        log.debug("could not prune stale table embeddings: %s", e)
    idx.table_collection.upsert(ids=table_names, embeddings=embeddings, documents=texts)

    # ── BM25 index ───────────────────────────────────────────────────────
    idx.bm25_index       = BM25Okapi([_tokenize(t) for t in texts])
    idx.bm25_table_names = table_names
    _INDEXES[src.id] = idx

    # ── query-history dense index ────────────────────────────────────────
    _refresh_history_collection(client, src)

    elapsed = time.monotonic() - t0
    log.info(
        "[%s] %d table cards, built dense+BM25 indexes in %.2fs",
        src.id, len(cards), elapsed,
    )


# ── query history index ────────────────────────────────────────────────────

def _refresh_history_collection(client: Any | None = None,
                                src: "data_sources.DataSource | None" = None) -> None:
    src = src or data_sources.active()
    c = client or _chroma_client
    if c is None:
        return

    idx = _INDEXES.setdefault(src.id, _Index())
    # Rebuilt from scratch at every start, to exactly the usable rows: an
    # example retired since (thumbs-down, purge) must not linger, and deleting
    # ids from Chroma's HNSW index in place left it answering "Error finding
    # id" to every query, which silently took all examples away.
    try:
        c.delete_collection(src.history_collection)
    except Exception:
        pass
    idx.history_collection = c.get_or_create_collection(
        name=src.history_collection,
        metadata={"hnsw:space": "cosine"},
    )

    rows = get_example_rows(limit=800, source=src.id)
    if not rows:
        return

    try:
        embeddings = _embed([q for _, q, _, _, _ in rows])
    except Exception as e:
        log.warning("Failed to embed query history: %s", e)
        return

    idx.history_collection.upsert(
        ids=[str(row_id) for row_id, _, _, _, _ in rows],
        embeddings=embeddings,
        documents=[q for _, q, _, _, _ in rows],
        metadatas=[_example_meta(sql, verified, origin) for _, _, sql, verified, origin in rows],
    )
    log.info("[%s] indexed %d query history entries (%d verified)",
             src.id, len(rows), sum(1 for r in rows if r[3]))


def _example_meta(sql: str, verified, origin: str) -> dict:
    return {"sql": sql, "success": True, "verified": int(bool(verified)), "origin": origin or "auto"}


def refresh_query_history_index() -> None:
    """Incrementally upsert the newest successful query of the active source."""
    src = data_sources.active()
    history = _index(src.id).history_collection
    if history is None or _chroma_client is None:
        return

    pairs = get_successful_pairs(limit=1, source=src.id)
    if not pairs:
        return

    row_id, question, sql = pairs[0]
    str_id = str(row_id)

    try:
        existing = history.get(ids=[str_id])
        if existing and existing.get("ids"):
            return
    except Exception:
        pass

    try:
        embedding = _embed([question])
        history.upsert(
            ids=[str_id],
            embeddings=embedding,
            documents=[question],
            metadatas=[_example_meta(sql, False, "auto")],
        )
        log.debug("Indexed new query history entry id=%s", str_id)
    except Exception as e:
        log.warning("Failed to index query history entry id=%s: %s", str_id, e)


def remember_example(query_id: int, source_id: str | None = None) -> None:
    """(Re)index one example with its current standing (after a thumbs-up)."""
    history = _index(source_id).history_collection
    row = get_example(query_id)
    if history is None or row is None:
        return
    _, question, sql, verified, origin = row
    try:
        history.upsert(ids=[str(query_id)], embeddings=_embed([question]), documents=[question],
                       metadatas=[_example_meta(sql, verified, origin)])
    except Exception as e:
        log.debug("could not re-index example %s: %s", query_id, e)


def forget_example(query_id: int, source_id: str | None = None) -> None:
    """Remove a past query from the few-shot index (the user rated its answer down)."""
    history = _index(source_id).history_collection
    if history is None:
        return
    # Marked, not deleted: retrieval asks for success=True, and deleting ids in
    # place has corrupted the HNSW index before. The next start rebuilds it.
    try:
        got = history.get(ids=[str(query_id)])
        if got and got.get("ids"):
            meta = {**(got["metadatas"][0] or {}), "success": False}
            history.update(ids=[str(query_id)], metadatas=[meta])
    except Exception as e:
        log.debug("could not retire example %s: %s", query_id, e)


# ── public retrieval API ───────────────────────────────────────────────────

def retrieve_tables(question: str, k: int = 5) -> tuple[list[TableCard], bool]:
    """Hybrid BM25 + dense retrieval with reciprocal rank fusion + 1-hop FK closure.

    Returns (selected_tables, used_llm_fallback). The fallback flag is True only when
    primary retrieval found 0 tables and the routing LLM was invoked to recover.
    """
    src = data_sources.active()
    cards = metadata_loader.cards(src.id)
    idx = _index(src.id)
    _bm25_index, _bm25_table_names = idx.bm25_index, idx.bm25_table_names

    if idx.table_collection is None or _bm25_index is None:
        log.warning("[%s] indexes not ready — using BM25-only fallback (top-10)", src.id)
        return _bm25_fallback(question, 10, cards, idx), False

    n = len(_bm25_table_names)

    # Dense
    min_dense_dist = 1.0
    try:
        q_emb        = _embed([question])[0]
        dense_res    = idx.table_collection.query(query_embeddings=[q_emb], n_results=min(20, n))
        dense_ids    = dense_res["ids"][0] if dense_res["ids"] else []
        dense_dists  = dense_res.get("distances", [[]])[0] if dense_res else []
        min_dense_dist = min(dense_dists) if dense_dists else 1.0
    except Exception as e:
        log.warning("Dense retrieval failed: %s", e)
        dense_ids = []

    # BM25
    scores = _bm25_index.get_scores(_tokenize(question))
    scores = [
        s * 0.3 if any(_bm25_table_names[i].startswith(p) for p in _AUDIT_PREFIXES) else s
        for i, s in enumerate(scores)
    ]
    max_bm25_score = max(scores) if n > 0 else 0.0
    bm25_ranked    = sorted(range(n), key=lambda i: scores[i], reverse=True)[:20]
    bm25_ids       = [_bm25_table_names[i] for i in bm25_ranked]

    log.info("[retrieval] dense: %s", ", ".join(dense_ids[:8]))
    log.info("[retrieval] bm25:  %s", ", ".join(bm25_ids[:8]))
    log.info("[retrieval] confidence: max_bm25=%.4f  min_dense_dist=%.4f", max_bm25_score, min_dense_dist)

    # Reciprocal rank fusion
    fused: dict[str, float] = {}
    for rank, tid in enumerate(dense_ids):
        fused[tid] = fused.get(tid, 0.0) + 1.0 / (60 + rank + 1)
    for rank, tid in enumerate(bm25_ids):
        fused[tid] = fused.get(tid, 0.0) + 1.0 / (60 + rank + 1)

    top_k = sorted(fused, key=lambda t: fused[t], reverse=True)[:k]

    # BM25 anchor: when BM25 is highly confident (strong token match, e.g. Indonesian synonym
    # hit on m_destination "tujuan"), guarantee its top result is in the final set even if
    # the English-only dense model disagrees completely.
    if max_bm25_score >= 5.0:
        bm25_winner = _bm25_table_names[bm25_ranked[0]]
        if bm25_winner not in top_k:
            top_k = [bm25_winner] + top_k[: k - 1]

    # 1-hop FK closure, capped at +3
    top_set = set(top_k)
    extra: list[str] = []
    for name in top_k:
        card = cards.get(name)
        if not card:
            continue
        for col in card.columns:
            if col.fk_ref:
                ref_table, _ = col.fk_ref
                if ref_table not in top_set and ref_table not in extra:
                    extra.append(ref_table)
                    if len(extra) >= 3:
                        break
        if len(extra) >= 3:
            break

    top_k = top_k + extra[:3]
    log.info("[retrieval] fused top-%d: %s", k, ", ".join(top_k))

    selected_tables = [cards[name] for name in top_k if name in cards]

    # Off-domain check: fast keyword match for known plantation terms; LLM call only for edge cases.
    _off_domain = _is_off_domain(question)

    if len(selected_tables) == 0 or _off_domain:
        log.warning(
            "LLM fallback triggered: off_domain=%s max_bm25=%.4f min_dist=%.4f query=%r",
            _off_domain, max_bm25_score, min_dense_dist, question[:80],
        )
        table_list = "\n".join(
            f"{name}: {card.description}"
            for name, card in sorted(cards.items())
        )
        from prompts import load_prompt
        messages = [
            {
                "role": "system",
                "content": load_prompt("table_selector_system", table_list=table_list),
            },
            {
                "role": "user",
                "content": f"User query: {question}",
            },
        ]
        llm_tables = call_routing_llm(messages)
        valid_names = [name for name in llm_tables if name in cards][:5]
        selected_tables = [cards[name] for name in valid_names]
        return selected_tables, True

    return selected_tables, False


def _bm25_fallback(question: str, k: int, cards: dict[str, TableCard],
                   idx: _Index | None = None) -> list[TableCard]:
    idx = idx or _index()
    _bm25_index, _bm25_table_names = idx.bm25_index, idx.bm25_table_names
    if not _bm25_index:
        return []
    scores = _bm25_index.get_scores(_tokenize(question))
    scores = [
        s * 0.3 if any(_bm25_table_names[i].startswith(p) for p in _AUDIT_PREFIXES) else s
        for i, s in enumerate(scores)
    ]
    ranked  = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
    return [cards[_bm25_table_names[i]] for i in ranked if _bm25_table_names[i] in cards]


# Example selection. Cosine distances from bge-small are compressed: on this
# history a question about the same table and measure sits at 0.13-0.28, and
# an unrelated one ("mandays on pruning" against "total harvest last 30 days")
# from about 0.26. An unrelated example is worse than none, so past this the
# candidate is dropped. A verified example (hand-written seed or thumbs-up)
# ranks as if VERIFIED_BONUS closer, so it wins a near tie but not against a
# near-identical question.
EXAMPLE_MAX_DISTANCE = 0.30
VERIFIED_BONUS = 0.05
# The date filter is the part of an example the writer copies most readily. An
# example whose question names a period ("this year") shown for one that names
# none taught it to drop the default period (staging rule 10) on 3 of 3 runs,
# so a mismatch on "is a period named" ranks the example this much further away.
PERIOD_MISMATCH_PENALTY = 0.06
_EXAMPLE_CANDIDATES = 12


def retrieve_examples(question: str, k: int = 3, *, min_distance: float = 0.0) -> list[tuple[str, str]]:
    """Return up to k (past_question, past_sql) pairs from the active source's history.

    Candidates are the nearest stored questions within EXAMPLE_MAX_DISTANCE.
    Any whose SQL breaks a data rule (lint) or names a table or column that no
    longer exists (schema check) is skipped, and so is a repeat of an SQL
    already chosen. `min_distance` drops near-copies of the question itself
    (sql_eval uses it so a test cannot be answered by its own stored answer).
    Past SQL is never shared across sources: Postgres examples would teach the
    T-SQL source the wrong dialect.
    """
    history = _index().history_collection
    if history is None:
        return []

    try:
        total = history.count()
    except Exception:
        return []
    if total == 0:
        return []

    try:
        q_emb   = _embed([question])[0]
        results = history.query(
            query_embeddings=[q_emb],
            n_results=min(_EXAMPLE_CANDIDATES, total),
            where={"success": True},
        )
        if not results["ids"] or not results["ids"][0]:
            return []

        src = data_sources.active()
        import sql_schema_check
        from chat_conversation import names_period

        asks_period = names_period(question)
        candidates = []
        for pq, meta, dist in zip(results["documents"][0], results["metadatas"][0],
                                  results["distances"][0]):
            # Chroma reports an exact match as a tiny negative distance (-0.000),
            # so the lower bound only applies when one was asked for.
            if dist > EXAMPLE_MAX_DISTANCE or (min_distance > 0 and dist < min_distance):
                continue
            score = dist - (VERIFIED_BONUS if meta.get("verified") else 0.0)
            if names_period(pq) != asks_period:
                score += PERIOD_MISMATCH_PENALTY
            candidates.append((score, pq, meta["sql"]))
        candidates.sort(key=lambda c: c[0])

        pairs: list[tuple[str, str]] = []
        seen_sql: set[str] = set()
        for _, pq, sql in candidates:
            key = re.sub(r"\s+", " ", sql.strip().lower())
            if key in seen_sql:
                continue
            if src.lint and src.lint(sql, pq):
                continue
            if sql_schema_check.check(sql, src.dialect, src.id):
                continue
            seen_sql.add(key)
            pairs.append((pq, sql))
            if len(pairs) >= k:
                break
        log.info("[retrieval] examples: %d of %d candidates", len(pairs), len(candidates))
        return pairs
    except Exception as e:
        log.warning("Example retrieval failed: %s", e)
        return []
