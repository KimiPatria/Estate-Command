"""
Manual smoke test for the RAG source toggle (item #7 of the task brief).

Runs the same sample questions through all three modes — "sql",
"knowledge_base", "both" — against the running dashboard server, and prints
the answer plus the per-request instrumentation (llm_calls, tokens, latency)
from rag_calls.jsonl side by side, so results are easy to eyeball before the
golden-set eval harness exists.

Requires the server running first:
    C:\\Users\\DP\\anaconda3\\python.exe -m uvicorn dashboard_server:app --port 8001

Usage:
    C:\\Users\\DP\\anaconda3\\python.exe rag_toggle_demo.py
    C:\\Users\\DP\\anaconda3\\python.exe rag_toggle_demo.py --base-url http://127.0.0.1:8001

knowledge_base / both calls are expected to fail gracefully (BEDROCK_KB_ID is
unset until infra/terraform/bedrock_kb.tf is applied) — this script exercises
the toggle mechanics and error handling, not a live Bedrock Knowledge Base.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

import httpx

from config import RAG_LOG_PATH

SOURCES = ["sql", "knowledge_base", "both"]

# Mix of questions the SQL path should answer from the EPMS DB and questions
# shaped for the KB's dummy contract documents (doc_ingestion/samples).
QUESTIONS = [
    "Total harvest last 30 days",
    "How many active employees per estate?",
    "What does the force majeure clause say about delivery delays?",
    "What's in the rate card for logistics?",
]


def _tail_jsonl(path: str, n: int = 200) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def run(base_url: str) -> None:
    try:
        httpx.get(base_url + "/health", timeout=3)
    except Exception as exc:
        print(f"Server not reachable at {base_url}: {exc}")
        print("Start it first: python -m uvicorn dashboard_server:app --port 8001")
        sys.exit(1)

    session_ids: dict[tuple[str, str], str] = {}
    results: list[dict] = []

    for question in QUESTIONS:
        print(f"\n{'=' * 100}\nQ: {question}\n{'=' * 100}")
        for source in SOURCES:
            # Fresh session per (question, source) so modes don't leak context
            # into each other — each source is tested independently.
            sid = str(uuid.uuid4())
            session_ids[(question, source)] = sid
            try:
                resp = httpx.post(
                    base_url + "/chat/message",
                    json={"message": question, "session_id": sid, "source": source},
                    timeout=60,
                )
                data = resp.json()
            except Exception as exc:
                data = {"answer": f"(request failed: {exc})", "error_type": "request_failed"}

            answer = (data.get("answer") or "")[:220]
            print(f"\n  [{source}] route={data.get('route')} error_type={data.get('error_type')}")
            print(f"  {answer}")
            results.append({"question": question, "source": source, "session_id": sid, "data": data})

    # ── side-by-side instrumentation from rag_calls.jsonl ───────────────────
    print(f"\n\n{'=' * 100}\nInstrumentation (from {RAG_LOG_PATH})\n{'=' * 100}")
    log_lines = _tail_jsonl(RAG_LOG_PATH)
    by_session = {}
    for rec in log_lines:
        by_session.setdefault(rec.get("session_id"), []).append(rec)

    header = f"{'question':<45} {'source':<15} {'ok':<6} {'calls':<6} {'in_tok':<8} {'out_tok':<8} {'latency_ms':<10}"
    print(header)
    print("-" * len(header))
    for r in results:
        recs = by_session.get(r["session_id"], [])
        rec = recs[-1] if recs else {}
        q = r["question"][:43]
        print(
            f"{q:<45} {r['source']:<15} {str(rec.get('ok', '?')):<6} "
            f"{str(rec.get('llm_calls', '-')):<6} {str(rec.get('input_tokens', '-')):<8} "
            f"{str(rec.get('output_tokens', '-')):<8} {str(rec.get('latency_ms', '-')):<10}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    run(args.base_url)
