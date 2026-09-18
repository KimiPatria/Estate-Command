"""The narrative layers over Estate Command: block, fire, handover, artifact, interview.

Each function here follows the same contract, the one forecast_intelligence.py
established for the forecast page:

    the server computes the numbers, the model writes the sentence.

Nothing in this module lets a model produce a figure. The grounding payload is
assembled from layers.py, fire.py, decisions.py and readiness.py, handed over
with an explicit provenance tag on every block of it, and the prompts forbid
deriving anything new. What comes back is prose and an ordering. The UI keeps
rendering the server's figures beside it, so a model that hallucinates a number
is visibly contradicted by the panel it sits in rather than believed.

Every function degrades to a payload with ``available: false`` and a reason.
The map, the panels and the drawer all render without any of this.
"""

import logging

from gis import decisions, layers, ontology, readiness, reasoning, vegetation
from prompts import load_prompt

log = logging.getLogger("estate-command.briefings")

# The fire situation moves; a block's five-month harvest history does not.
_BLOCK_CACHE = reasoning.TTLCache(ttl_seconds=6 * 3600, maxsize=200)
_FIRE_CACHE = reasoning.TTLCache(ttl_seconds=15 * 60, maxsize=12)
_HANDOVER_CACHE = reasoning.TTLCache(ttl_seconds=10 * 60, maxsize=8)
_INTERVIEW_CACHE = reasoning.TTLCache(ttl_seconds=24 * 3600, maxsize=32)


# How much a status claims, so a proposal that claims less can be held to a
# higher bar than one that claims more.
_SEVERITY = {"ready": 3, "partial": 2, "synthetic": 1, "unavailable": 0}


def _pct(v, digits=2):
    return round(v * 100, digits) if isinstance(v, (int, float)) else None


def _audit(parsed: dict, grounding: dict) -> dict:
    """Check the brief's own figures against the payload it was handed.

    Same guardrail the copilot runs on its answers. A brief that quotes a
    number nobody measured is the one failure that would make this page worse
    than no page, so it is checked rather than trusted - and the check is
    shown, not hidden, because a viewer who can see the audit can trust the
    briefs that pass it.
    """
    text = " ".join(str(v) for v in _walk_strings(parsed))
    return reasoning.audit_figures(text, grounding)


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_strings(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _walk_strings(value)


# ── UC-03 / the drawer: one block ──────────────────────────────────────────

def block_grounding(estate: str, block_id: str, month: str | None = None) -> dict | None:
    """Everything known about one block, tagged by where it came from.

    This is also the payload the UI shows under "what the model was given", so
    a viewer can check the brief against its own inputs. Keep it readable.
    """
    rows = layers.block_rows(estate, month)
    if rows is None:
        return None
    row = next((r for r in rows if r["block_id"] == block_id), None)
    if row is None:
        return None

    cohort = [r for r in rows if r["planted_year"] == row["planted_year"]]
    ranked = sorted((r for r in cohort if r["bunches_per_ha"] is not None),
                    key=lambda r: r["bunches_per_ha"], reverse=True)
    rank = next((i + 1 for i, r in enumerate(ranked) if r["block_id"] == block_id), None)

    ndre_cohort = sorted(r["ndre"] for r in cohort if r["ndre"] is not None)
    scene = vegetation.scene(estate) or {}

    not_recorded = [k for k, v in (
        ("tonnage (no average bunch weight in the export)", row["abw_kg"]),
        ("last harvest date", row["last_harvest_date"]),
        ("cost ledger", row["cost_idr"]),
    ) if v is None]

    return {
        "block": {
            "label": row["block_label"], "division": row["division_code"],
            "estate": estate.upper(), "planted_year": row["planted_year"],
            "palm_age_years": row["palm_age_years"], "planted_ha": row["planted_ha"],
            "palms": row["palms"], "provenance": "real: client ArcGIS export",
        },
        "window": month or "full recorded window",
        "harvest": {
            "bunches": row["bunches"], "bunches_per_ha": row["bunches_per_ha"],
            "harvest_days": row.get("harvest_days"),
            "deduction_rate_pct": _pct(row["deduction_rate"]),
            "is_forecast_month": row["is_forecast"],
            "provenance": ("synthetic: forward forecast" if row["is_forecast"]
                           else "real: EPMS OPH records"),
        },
        "peers": {
            "cohort_planting_year": row["planted_year"],
            "cohort_size": len(cohort),
            "peer_index": row["peer_index"],
            "rank_in_cohort": rank,
            "reading": "1.00 is the cohort median; below 0.85 is materially behind",
            "controls_for": "planting year only. Soil, terrain, drainage and "
                            "rainfall are not in this data.",
            "provenance": "real: derived from the client's own harvest records",
        },
        "canopy": {
            "ndre": row.get("ndre"),
            "ndre_anomaly_vs_cohort": row.get("ndre_anomaly"),
            "ndvi": row.get("ndvi"),
            "cohort_ndre_median": (ndre_cohort[len(ndre_cohort) // 2]
                                   if ndre_cohort else None),
            "pixels_clear_pct": row.get("ndre_valid_pct"),
            "scene_date": row.get("ndre_date") or scene.get("date"),
            "provenance": ("satellite: Sentinel-2 L2A, measured over the client's "
                           "polygons" if row.get("ndre_source") == "real:sentinel-2"
                           else "synthetic: generated vigour series"),
        },
        "operational": {
            "ripeness_pressure": row["ripeness_pressure"],
            "days_since_harvest": row["days_since_harvest"],
            "rotation_target_days": row["rotation_target_days"],
            "gang": row["gang_code"],
            "upkeep_days_overdue": row["upkeep_overdue"],
            "tonnes": row["tonnes"], "cost_per_kg_idr": row["cost_per_kg"],
            "margin_per_ha_m_idr": row["margin_per_ha"],
            "provenance": "SYNTHETIC. EPMS records none of these for this export.",
        },
        "not_recorded": not_recorded,
    }


def block_brief(estate: str, block_id: str, month: str | None = None,
                refresh: bool = False) -> dict:
    key = (estate.upper(), block_id, month)
    if not refresh:
        hit = _BLOCK_CACHE.get(key)
        if hit:
            return dict(hit, cached=True)

    grounding = block_grounding(estate, block_id, month)
    if grounding is None:
        return reasoning.unavailable(f"No block {block_id!r} on estate {estate!r}.")

    parsed, meta = reasoning.json_chat(
        load_prompt("estate_block_brief_system"),
        "Brief this block.\n\n" + reasoning.clip(grounding),
        tier="routing", max_tokens=900, temperature=0.2, task="estate_block_brief")
    if parsed is None:
        return reasoning.unavailable(
            meta.get("error", "The model did not return usable JSON."),
            grounding=grounding, meta=meta)

    severity = str(parsed.get("severity", "")).lower()
    out = {
        "available": True,
        "block_id": block_id,
        "headline": str(parsed.get("headline", "")).strip(),
        "severity": severity if severity in ("watch", "investigate", "routine") else "routine",
        "narrative": str(parsed.get("narrative", "")).strip(),
        "action": str(parsed.get("action", "")).strip(),
        "evidence": [str(e) for e in (parsed.get("evidence") or [])][:5],
        "not_recorded": [str(e) for e in (parsed.get("not_recorded") or [])][:5],
        "figure_audit": _audit(parsed, grounding),
        "grounding": grounding,
        "model": meta.get("model"),
        "latency_ms": meta.get("latency_ms"),
        "usage": meta.get("usage"),
        "cached": False,
    }
    _BLOCK_CACHE.put(key, out)
    return out


# ── UC-06 / the fire panel ─────────────────────────────────────────────────

def fire_grounding(assessment: dict, estate: str) -> dict:
    """Trim a full fire assessment to what a brief needs, keeping the split.

    The provenance strings here are read off the payload, never asserted. Under
    the live scenario the hotspots are NASA's and the wind is Open-Meteo's;
    under the near-miss and severe rehearsals both are overridden with invented
    values. A brief that called an invented 21 km/h wind "real, live" would be
    the exact failure this page is built to avoid, and hard-coding the string
    is how that happens.
    """
    h = assessment.get("hotspots", {}) or {}
    wx = assessment.get("weather", {}) or {}
    wind = assessment.get("wind", {}) or {}
    ex = assessment.get("exposure", {}) or {}
    mob = assessment.get("mobilisation", {}) or {}
    threatened = assessment.get("threatened_blocks") or []
    clusters = h.get("clusters") or []
    threatening = [c for c in clusters if c.get("threatens_blocks")]

    def prov(tag: str, real_label: str) -> str:
        return (real_label if str(tag or "").startswith("real")
                else f"SYNTHETIC ({tag or 'scenario override'})")

    return {
        "estate": estate.upper(),
        "scenario": assessment.get("scenario"),
        "scenario_label": assessment.get("scenario_label"),
        "detection": {
            "source": h.get("source"),
            "clusters_total": h.get("clusters_total"),
            "threatening": h.get("threatening"),
            "detections": h.get("detections"),
            "nearest_km": min((c["distance_km"] for c in clusters
                               if c.get("distance_km") is not None), default=None),
            "nearest_threatening_km": min((c["distance_km"] for c in threatening
                                           if c.get("distance_km") is not None),
                                          default=None),
            "frp_total_mw": (round(sum(c.get("frp_total") or 0 for c in threatening), 1)
                             if threatening else None),
            "largest_cluster": (max(threatening, key=lambda c: c.get("frp_total") or 0)
                                if threatening else None),
            "provenance": prov(h.get("provenance"),
                               "REAL, live: NASA FIRMS VIIRS detections"),
        },
        "weather": {
            "wind_kmh": wind.get("speed_kmh"),
            "wind_from_deg": wind.get("from_deg"),
            "humidity_pct": wx.get("humidity_pct"),
            "rain_14d_mm": wx.get("rain_14d_mm"),
            "dry_days_14d": wx.get("dry_days_14d"),
            "dryness_index": (assessment.get("model") or {}).get("dryness"),
            "wind_provenance": prov(wind.get("provenance"), "REAL, live: Open-Meteo"),
            "observation_provenance": prov(wx.get("provenance"),
                                           "REAL, live: Open-Meteo"),
        },
        "exposure": {
            "blocks": ex.get("blocks"),
            "planted_hectares": ex.get("planted_ha"),
            "palms": ex.get("palms"),
            "by_band": ex.get("by_band"),
            "valuation": ex.get("valuation"),
            "valuation_note": ex.get("valuation_note"),
            "provenance": "REAL: the client's own polygons, planted area and palm counts",
        },
        "threatened_blocks": [{
            "block": t.get("block_label"), "division": t.get("division_code"),
            "band": t.get("band"), "eta_hours": t.get("eta_hours"),
            "distance_km": t.get("distance_km"),
            "bearing_offset_deg": t.get("bearing_offset_deg"),
            "planted_ha": t.get("planted_ha"), "palms": t.get("palms"),
            "frp_total_mw": t.get("frp_total"),
        } for t in threatened[:12]],
        "threatened_blocks_shown": min(len(threatened), 12),
        "threatened_blocks_total": len(threatened),
        "response_assets": {
            "mobilisation_required": mob.get("required"),
            "target_block": mob.get("target_block"),
            "eta_fire_hours": mob.get("eta_fire_hours"),
            "crew_reachable_in_window": mob.get("crew_reachable_in_window"),
            "margin_hours": mob.get("margin_hours"),
            "nearest_post": (mob.get("post") or {}).get("name"),
            "crew_on_shift": (mob.get("post") or {}).get("crew_on_shift"),
            "travel_minutes": (mob.get("post") or {}).get("travel_minutes"),
            "road_km": (mob.get("post") or {}).get("road_km"),
            "water_source": (mob.get("water") or {}).get("name"),
            "provenance": "SYNTHETIC. EPMS has no table that can hold a fire post, "
                          "water source or shift roster. Invented for this demo.",
        },
        "model": assessment.get("model"),
    }


def fire_brief(assessment: dict, estate: str, refresh: bool = False) -> dict:
    key = (estate.upper(), assessment.get("scenario"),
           (assessment.get("hotspots") or {}).get("clusters_total"),
           (assessment.get("exposure") or {}).get("blocks"))
    if not refresh:
        hit = _FIRE_CACHE.get(key)
        if hit:
            return dict(hit, cached=True)

    grounding = fire_grounding(assessment, estate)
    if not grounding["exposure"].get("blocks"):
        # Nothing is threatened. Say so from the server; a model is not needed
        # to write "no action" and should not be paid to.
        return {
            "available": True, "model": None, "generated": False,
            "headline": "No blocks are in the path of a detected hotspot.",
            "posture": "monitor", "assessment": "",
            "priority": [], "orders": [], "blocked_by_missing_data": [],
            "grounding": grounding, "cached": False,
        }

    parsed, meta = reasoning.json_chat(
        load_prompt("estate_fire_brief_system"),
        "Write the fire brief.\n\n" + reasoning.clip(grounding),
        tier="main", max_tokens=1200, temperature=0.2, task="estate_fire_brief")
    if parsed is None:
        return reasoning.unavailable(
            meta.get("error", "The model did not return usable JSON."),
            grounding=grounding, meta=meta)

    posture = str(parsed.get("posture", "")).lower()
    priority = []
    for p in (parsed.get("priority") or [])[:5]:
        if isinstance(p, dict):
            priority.append({"block": str(p.get("block", "")),
                             "why": str(p.get("why", ""))})
    out = {
        "available": True, "generated": True,
        "headline": str(parsed.get("headline", "")).strip(),
        "posture": posture if posture in ("monitor", "prepare", "mobilise") else "prepare",
        "assessment": str(parsed.get("assessment", "")).strip(),
        "priority": priority,
        "orders": [str(o) for o in (parsed.get("orders") or [])][:5],
        "blocked_by_missing_data": [str(o) for o in
                                    (parsed.get("blocked_by_missing_data") or [])][:5],
        "figure_audit": _audit(parsed, grounding),
        "grounding": grounding,
        "model": meta.get("model"), "latency_ms": meta.get("latency_ms"),
        "usage": meta.get("usage"), "cached": False,
    }
    _FIRE_CACHE.put(key, out)
    return out


# ── UC-14 / the decision log: shift handover ───────────────────────────────

def handover_grounding(estate: str = "EC") -> dict:
    contract = layers.contract_position(estate)
    rotation = layers.rotation_plan(estate, 5)
    labour = layers.labour_position(estate)
    replant = layers.replant_schedule(estate)
    log_rows = decisions.history(30, estate)
    reg = readiness.catalogue()

    worst = contract.get("worst_forward_month") or {}
    return {
        "estate": estate.upper(),
        "positions": {
            "contract": {
                "months_short_at_p50": contract.get("months_in_p50_deficit"),
                "months_short_at_p10": contract.get("months_in_p10_deficit"),
                "worst_month": worst.get("month"),
                "worst_gap_t": worst.get("gap_t"),
                "provenance": "SYNTHETIC: committed volumes, tonnage and the "
                              "forward forecast are all generated.",
            },
            "rotation": {
                "overdue_blocks": rotation.get("overdue_blocks"),
                "overdue_hectares": rotation.get("overdue_ha"),
                "top_blocks": [{"block": b.get("block_label"),
                                "gang": b.get("gang_code"),
                                "ripeness_pressure": b.get("ripeness_pressure"),
                                "days_since_harvest": b.get("days_since_harvest"),
                                "rotation_target_days": b.get("rotation_target_days")}
                               for b in (rotation.get("queue") or [])[:5]],
                "provenance": "SYNTHETIC: last-harvest dates and gangs are generated.",
            },
            "labour": {
                "worst_deficit": labour.get("worst_deficit"),
                "worst_month": labour.get("worst_month"),
                "provenance": "SYNTHETIC: attendance and establishment are generated.",
            },
            "replant": {
                "peak_year": replant.get("peak_year"),
                "peak_pct_of_estate": replant.get("peak_pct"),
                "finding": replant.get("finding"),
                "provenance": "REAL: the client's own planting years.",
            },
            "canopy": ({"scene_date": (vegetation.scene(estate) or {}).get("date"),
                        "blocks_measured": (vegetation.summary(estate)
                                            .get("coverage") or {}).get("measured"),
                        "provenance": "REAL: Sentinel-2 over the client's polygons"}
                       if vegetation.available(estate) else None),
        },
        "decision_log": {
            "total": log_rows.get("total"),
            "by_action": log_rows.get("by_action"),
            "entries": [{"at": d["created_at"], "action": d["action"],
                         "title": d["title"], "subject": d["subject"],
                         "use_case": d["use_case"], "actor": d.get("actor"),
                         "artifact": (d.get("artifact") or {}).get("reference")}
                        for d in (log_rows.get("decisions") or [])[:15]],
            "note": "Artifacts were drafted only. Nothing was written to EPMS "
                    "and nothing was sent.",
        },
        "data_readiness": reg["summary"],
    }


def handover(estate: str = "EC", refresh: bool = False) -> dict:
    grounding = handover_grounding(estate)
    key = (estate.upper(), grounding["decision_log"]["total"])
    if not refresh:
        hit = _HANDOVER_CACHE.get(key)
        if hit:
            return dict(hit, cached=True)

    parsed, meta = reasoning.json_chat(
        load_prompt("estate_handover_system"),
        "Write the handover note.\n\n" + reasoning.clip(grounding),
        tier="main", max_tokens=1100, temperature=0.25, task="estate_handover")
    if parsed is None:
        return reasoning.unavailable(
            meta.get("error", "The model did not return usable JSON."),
            grounding=grounding, meta=meta)

    out = {
        "available": True,
        "headline": str(parsed.get("headline", "")).strip(),
        "decided": [str(x) for x in (parsed.get("decided") or [])][:6],
        "open": [str(x) for x in (parsed.get("open") or [])][:6],
        "watch": [str(x) for x in (parsed.get("watch") or [])][:6],
        "note": str(parsed.get("note", "")).strip(),
        "figure_audit": _audit(parsed, grounding),
        "grounding": grounding,
        "model": meta.get("model"), "latency_ms": meta.get("latency_ms"),
        "usage": meta.get("usage"), "cached": False,
    }
    _HANDOVER_CACHE.put(key, out)
    return out


# ── 4.2 / the artifact: the words on the document ──────────────────────────

def artifact_prose(doc: dict, evidence: list | None = None,
                   provenance_note: str | None = None) -> dict | None:
    """Write the instruction text for a drafted artifact.

    Returns None on any failure. The artifact is a deterministic document that
    stands on its own; this only adds the paragraph a supervisor reads first,
    and its absence must never block a decision from being recorded.
    """
    kind_meta = decisions.ARTIFACT_KINDS.get(doc.get("kind")) or {}
    payload = {
        "document": {k: doc.get(k) for k in
                     ("kind", "label", "title", "summary", "reference", "estate",
                      "would_route_to", "would_create", "approver_role", "status")},
        "lines": (doc.get("lines") or [])[:10],
        "triggered_by": evidence or [],
        # Per-kind, because a blanket "may be synthetic" made the model call a
        # real peer index invented - an error in the opposite direction, and
        # just as damaging to a page whose whole claim is that it knows which
        # half of itself is real.
        "provenance": (provenance_note or kind_meta.get("provenance") or
                       "Line values may be synthetic; see the panel that raised this."),
    }
    parsed, meta = reasoning.json_chat(
        load_prompt("estate_artifact_system"),
        "Write the instruction text for this draft.\n\n" + reasoning.clip(payload),
        tier="routing", max_tokens=700, temperature=0.2, task="estate_artifact")
    if parsed is None:
        log.info("[briefings] artifact prose unavailable: %s",
                 meta.get("error", "unparseable"))
        return None
    return {
        "purpose": str(parsed.get("purpose", "")).strip(),
        "instructions": [str(x) for x in (parsed.get("instructions") or [])][:5],
        "acceptance": str(parsed.get("acceptance", "")).strip(),
        "caveat": str(parsed.get("caveat", "")).strip(),
        "model": meta.get("model"),
        "note": "Drafted by the model from the document above. Nothing was sent.",
    }


# ── UC-15 / the readiness panel: the interview ─────────────────────────────

def interview_questions(cap_id: str, refresh: bool = False) -> dict:
    cap = readiness.get(cap_id)
    if not cap:
        return reasoning.unavailable(f"No capability {cap_id!r} in the register.")
    if not refresh:
        hit = _INTERVIEW_CACHE.get(cap_id)
        if hit:
            return dict(hit, cached=True)

    grounding = {k: cap.get(k) for k in
                 ("capability", "needs", "status", "evidence", "degrades_to", "ask")}
    parsed, meta = reasoning.json_chat(
        load_prompt("estate_interview_system"),
        "Prepare the questions for this capability.\n\n" + reasoning.clip(grounding),
        tier="routing", max_tokens=1000, temperature=0.35, task="estate_interview")
    if parsed is None:
        return reasoning.unavailable(
            meta.get("error", "The model did not return usable JSON."),
            capability=cap, meta=meta)

    questions = []
    for q in (parsed.get("questions") or [])[:4]:
        if isinstance(q, dict):
            questions.append({"ask": str(q.get("ask", "")).strip(),
                              "why_it_matters": str(q.get("why_it_matters", "")).strip(),
                              "listen_for": str(q.get("listen_for", "")).strip()})
    out = {
        "available": True,
        "capability_id": cap_id,
        "capability": cap["capability"],
        "status": cap["status"],
        "opening": str(parsed.get("opening", "")).strip(),
        "questions": questions,
        "cheapest_unlock": str(parsed.get("cheapest_unlock", "")).strip(),
        "if_unavailable": str(parsed.get("if_unavailable", "")).strip(),
        "model": meta.get("model"), "latency_ms": meta.get("latency_ms"),
        "usage": meta.get("usage"), "cached": False,
    }
    _INTERVIEW_CACHE.put(cap_id, out)
    return out


def score_answer(cap_id: str, answer: str, actor: str = "demo user") -> dict:
    """Score a client's answer against the measured row, then persist both."""
    cap = readiness.get(cap_id)
    if not cap:
        return reasoning.unavailable(f"No capability {cap_id!r} in the register.")
    if not (answer or "").strip():
        return reasoning.unavailable("No answer to score.")

    grounding = {
        "capability": cap["capability"], "needs": cap["needs"],
        "measured_status": cap["status"], "measured_evidence": cap["evidence"],
        "degrades_to": cap["degrades_to"], "standing_ask": cap["ask"],
        "client_answer": answer.strip(),
    }
    parsed, meta = reasoning.json_chat(
        load_prompt("estate_interview_score_system"),
        "Score this answer.\n\n" + reasoning.clip(grounding),
        tier="routing", max_tokens=800, temperature=0.15,
        task="estate_interview_score")

    if parsed is None:
        # Still record what the client said. The answer is the valuable half
        # and it is not the client's problem that the model fell over.
        stored = readiness.record_answer(cap_id, answer, None, actor)
        return reasoning.unavailable(
            meta.get("error", "The model did not return usable JSON."),
            recorded=True, stored=stored, meta=meta)

    proposed = str(parsed.get("proposed_status", "")).lower()
    if proposed not in readiness.STATUSES:
        proposed = cap["status"]
    confidence = str(parsed.get("confidence", "low")).lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    # A register row is a measurement. Low confidence never moves one, whatever
    # the model proposed - and a vague answer is exactly what produces low
    # confidence. The proposal is still shown, marked withheld, because "the
    # model wanted to move this and was not allowed to" is itself useful in
    # the room.
    downgrade = (_SEVERITY.get(proposed, 0) < _SEVERITY.get(cap["status"], 0))
    withheld_reason = None
    if proposed != cap["status"]:
        if confidence == "low":
            withheld_reason = "low confidence"
        elif downgrade and confidence != "high":
            # Downgrading is the expensive direction: it takes a capability off
            # the table for everyone downstream. A vague answer reliably comes
            # back "medium", so medium is not enough to demote a measurement.
            withheld_reason = "a downgrade needs high confidence"
    withheld = withheld_reason is not None
    if withheld:
        log.info("[briefings] withholding %s -> %s: %s",
                 cap["status"], proposed, withheld_reason)
        proposed = cap["status"]
    verdict = {
        "proposed_status": proposed,
        "changed": proposed != cap["status"],
        "withheld": withheld,
        "withheld_reason": withheld_reason,
        "withheld_proposal": (str(parsed.get("proposed_status", "")).lower()
                              if withheld else None),
        "confidence": confidence,
        "rationale": str(parsed.get("rationale", "")).strip(),
        "unlocks": [str(x) for x in (parsed.get("unlocks") or [])][:5],
        "next_ask": str(parsed.get("next_ask", "")).strip(),
        "verify_by": str(parsed.get("verify_by", "")).strip(),
        "model": meta.get("model"),
    }
    stored = readiness.record_answer(cap_id, answer, verdict, actor)
    return {
        "available": True, "recorded": True,
        "capability_id": cap_id, "capability": cap["capability"],
        "measured_status": cap["status"],
        "verdict": verdict, "stored": stored,
        "note": ("Advisory. The measured status is unchanged; the proposal is "
                 "shown beside it until someone verifies the claim."),
        "model": meta.get("model"), "latency_ms": meta.get("latency_ms"),
        "usage": meta.get("usage"),
    }


def clear_caches() -> dict:
    return {
        "block": _BLOCK_CACHE.clear(), "fire": _FIRE_CACHE.clear(),
        "handover": _HANDOVER_CACHE.clear(), "interview": _INTERVIEW_CACHE.clear(),
    }
