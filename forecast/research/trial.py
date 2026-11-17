"""Billing trial for Nova Web Grounding (buildplan_chat_centre.md C.6 #4 / C.7 step 1).

Runs a hard-capped set of real research queries (both regions, topics 1/3/4/6)
and saves the answers, citations and usage to forecast/research/trial_<date>.json.
About 24 h later, check Cost Explorer (service Amazon Bedrock, grouped by usage
type) that the grounding line went against the credit.

    python -m forecast.research.trial            # all 10
    python -m forecast.research.trial --max 2    # fewer
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from forecast.research import grounding

HARD_CAP = 10

SYSTEM = (
    "You research outside factors that could affect palm oil production for a "
    "plantation company. Search the web. Report only dated, sourced facts from the "
    "last few months; give the publication date of each. If nothing recent is "
    "found, say so plainly. Be brief."
)

QUERIES = [
    # (region, topic, question)
    ("south_papua", "climate",
     "BMKG prakiraan musim hujan / kemarau terbaru untuk Papua Selatan (Merauke, Boven Digoel), "
     "dan status ENSO dan IOD terkini menurut BMKG atau NOAA."),
    ("south_papua", "industry",
     "GAPKI latest monthly Indonesian palm oil production and stocks report, and current "
     "outlook for Indonesian CPO output in the next 6 months."),
    ("south_papua", "events",
     "Berita terbaru kebakaran hutan, kabut asap, banjir atau gangguan jalan di Merauke "
     "dan Boven Digoel, Papua Selatan."),
    ("south_papua", "prices",
     "Harga TBS kelapa sawit penetapan Disbun Papua Selatan atau Papua periode terbaru, "
     "dan harga referensi CPO serta pungutan ekspor terbaru."),
    ("south_papua", "events",
     "Recent news on oil palm pests or disease (Ganoderma, rhinoceros beetle, bagworm) or "
     "mill outages in Papua, Indonesia."),
    ("sarawak", "climate",
     "MetMalaysia latest seasonal weather outlook for Sarawak, northeast monsoon rainfall "
     "and flood risk for the coming months."),
    ("sarawak", "industry",
     "MPOB latest monthly palm oil production data for Malaysia and Sarawak, and the "
     "production outlook for the coming months."),
    ("sarawak", "events",
     "Recent news in Sarawak about floods, haze, or foreign worker shortages affecting "
     "oil palm plantations."),
    ("sarawak", "prices",
     "Latest FCPO crude palm oil futures price on Bursa Malaysia and Malaysia's CPO export "
     "duty reference price for next month."),
    ("both", "policy",
     "Latest news on EUDR implementation timing for palm oil and Indonesia's B40/B50 "
     "biodiesel mandate."),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=HARD_CAP)
    args = ap.parse_args(argv)
    n = max(0, min(args.max, HARD_CAP))
    sys.stdout.reconfigure(encoding="utf-8")

    out_path = Path(__file__).parent / f"trial_{date.today().isoformat()}.json"
    results = []
    for i, (region, topic, q) in enumerate(QUERIES[:n], 1):
        print(f"[{i}/{n}] {region} / {topic} ...", flush=True)
        try:
            g = grounding.ask(q, system=SYSTEM, task="research_trial", keep_raw=True)
        except Exception as exc:
            print(f"   FAILED: {type(exc).__name__}: {exc}")
            results.append({"region": region, "topic": topic, "query": q,
                            "error": f"{type(exc).__name__}: {exc}"})
            if "AccessDenied" in type(exc).__name__ or "AccessDenied" in str(exc):
                print("   Access denied: add bedrock:InvokeTool on amazon.nova_grounding. Stopping.")
                break
            continue
        print(f"   {g.latency_ms} ms, {len(g.citations)} citations, usage {g.usage}")
        results.append({"region": region, "topic": topic, "query": q, "text": g.text,
                        "citations": g.citations, "usage": g.usage, "latency_ms": g.latency_ms,
                        "raw_content_types": [list(b.keys())[0] for b in
                                              g.raw["output"]["message"]["content"]],
                        "raw_usage_keys": sorted(g.raw.get("usage", {}).keys()),
                        "metrics": g.raw.get("metrics")})
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for r in results if "error" not in r)
    print(f"\n{ok}/{len(results)} succeeded; saved {out_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
