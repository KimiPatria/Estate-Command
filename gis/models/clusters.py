"""Agronomic underperformance clustering.

The question an agronomy team cannot answer by eye: two blocks planted the same
year with the same material, side by side, and one yields several tonnes a
hectare less. Why?

`layers.compare_metrics` already answers the two-metric form of this - rank
correlation plus the blocks in the bottom fifth of both. This is the
n-dimensional generalisation: put every block in a space of vigour, yield, age,
nutrition, upkeep and disease, and find the groups.

What is real here
-----------------
More than anywhere else in the build. Canopy vigour is a real Sentinel-2
measurement over the client's own polygons, yield and planting year are the
client's own records, and elevation is a real Copernicus reading. Only
nutrition, upkeep and the pest census are stubbed. Four of the seven input
dimensions are measurements rather than inventions, and `feature_provenance`
in the payload says which are which.

Why HDBSCAN
-----------
K-means would force every block into a cluster and demand a k nobody can
justify. HDBSCAN finds groups of varying density and, more importantly, is
allowed to answer "this block belongs to no group" - which is the honest
answer for a block that is simply average. Noise points are reported as
unclustered rather than being quietly assigned somewhere.

How clusters are named, and the line this module will not cross
---------------------------------------------------------------
A cluster is labelled by which of its dimensions are extreme against the
estate mean: "low vigour, high disease, on-programme nutrition". That is a
description of the data and it is defensible.

It is NOT labelled "fertiliser deficit" or "drainage failure". Those are causal
claims the data cannot carry, and `gis/reasoning.audit_figures` would be right
to flag them. The distinction matters most exactly where it is most tempting:
the nutrition feed is deliberately confounded, because agronomists PRESCRIBE
more fertiliser to weak blocks, so a cluster that is both low-yielding and
high-fertiliser is showing a response to the problem and not its cause.
"""

import logging
from threading import Lock

import numpy as np

from gis import layers

log = logging.getLogger("estate-command.models.clusters")

_CACHE: dict = {}
_LOCK = Lock()

# The space blocks are clustered in. Each entry is (row key, short label,
# provenance) so the panel can badge every axis honestly.
# The yield axis is peer_index, not raw bunches per hectare. Clustering on the
# raw figure just rediscovers the age curve - the first run produced "high palm
# age, low yield", which is true, already known, and not actionable. peer_index
# is yield against blocks planted the same year, so a cluster that is low on it
# is underperforming its own cohort, which is the question the agronomy team
# actually cannot answer.
FEATURES = [
    ("ndre", "canopy vigour", "real"),
    ("peer_index", "yield vs cohort", "real"),
    ("palm_age_years", "palm age", "real"),
    ("elevation_m", "elevation", "real"),
    ("nutrient_gap_pct", "nutrition shortfall", "synthetic"),
    ("upkeep_overdue", "upkeep overdue", "synthetic"),
    ("ganoderma_pct", "ganoderma", "synthetic"),
]

# A dimension counts as characteristic of a cluster when the cluster mean sits
# this far from the estate mean, in estate standard deviations.
EXTREME_Z = 0.6

MIN_CLUSTER = 8          # blocks; smaller groups are noise on a 291-block estate


def _matrix(rows):
    keys = [f[0] for f in FEATURES]
    usable = [r for r in rows
              if all(isinstance(r.get(k), (int, float)) for k in keys)]
    X = np.array([[float(r[k]) for k in keys] for r in usable], dtype=float)
    return usable, X


def _fit(estate: str) -> dict | None:
    rows = layers.block_rows(estate)
    if not rows:
        return None
    usable, X = _matrix(rows)
    if len(usable) < MIN_CLUSTER * 2:
        return None

    mean, sd = X.mean(axis=0), X.std(axis=0)
    Z = (X - mean) / (sd + 1e-9)

    try:
        from sklearn.cluster import HDBSCAN
        model = HDBSCAN(min_cluster_size=MIN_CLUSTER, min_samples=4)
        labels = model.fit_predict(Z)
    except Exception as exc:
        log.warning("[clusters] HDBSCAN unavailable: %s", exc)
        return {"error": str(exc)}

    for r, lab in zip(usable, labels):
        r["_cluster"] = int(lab)

    return {"rows": usable, "X": X, "Z": Z, "labels": labels,
            "mean": mean, "sd": sd, "estate": estate.upper()}


def _state(estate: str = "EC"):
    key = estate.upper()
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        _CACHE[key] = _fit(estate)
        st = _CACHE[key]
        if st and "labels" in st:
            n = len(set(st["labels"]) - {-1})
            log.info("[clusters] %s: %d blocks, %d clusters, %d unclustered",
                     key, len(st["rows"]), n, int((st["labels"] == -1).sum()))
        return _CACHE[key]


def reload_clusters() -> None:
    with _LOCK:
        _CACHE.clear()


def _describe(z_means) -> tuple[str, list]:
    """Name a cluster by which axes are extreme. Description, never diagnosis."""
    traits = []
    for (key, label, prov), z in zip(FEATURES, z_means):
        if abs(z) < EXTREME_Z:
            continue
        traits.append({"feature": key, "label": label, "z": round(float(z), 2),
                       "direction": "high" if z > 0 else "low",
                       "provenance": prov})
    traits.sort(key=lambda t: -abs(t["z"]))
    if not traits:
        return "unremarkable on every axis", traits
    return ", ".join(f"{t['direction']} {t['label']}" for t in traits[:3]), traits


def assess(estate: str = "EC") -> dict:
    st = _state(estate)
    if st is None:
        return {"available": False,
                "reason": "Not enough blocks carry every input.",
                "features": [f[0] for f in FEATURES]}
    if "error" in st:
        return {"available": False, "reason": st["error"]}

    rows, Z, labels = st["rows"], st["Z"], st["labels"]
    est_mean = {f[0]: round(float(m), 3) for f, m in zip(FEATURES, st["mean"])}

    groups = []
    for lab in sorted(set(labels) - {-1}):
        idx = np.where(labels == lab)[0]
        members = [rows[i] for i in idx]
        z_means = Z[idx].mean(axis=0)
        name, traits = _describe(z_means)
        yields = [m["bunches_per_ha"] for m in members
                  if m.get("bunches_per_ha") is not None]
        peers = [m["peer_index"] for m in members if m.get("peer_index") is not None]
        groups.append({
            "cluster": int(lab),
            "blocks": len(idx),
            "label": name,
            "traits": traits,
            "planted_ha": round(sum(m.get("planted_ha") or 0 for m in members), 1),
            "mean_bunches_per_ha": round(sum(yields) / len(yields), 1) if yields else None,
            "mean_peer_index": round(sum(peers) / len(peers), 3) if peers else None,
            "means": {f[0]: round(float(st["X"][idx, i].mean()), 3)
                      for i, f in enumerate(FEATURES)},
            "divisions": sorted({m["division_code"] for m in members}),
            "block_labels": [m["block_label"] for m in members][:20],
        })

    # Rank on the age-controlled axis, the one the model actually clustered on.
    # Ranking on raw yield would put the oldest cluster last every time and say
    # nothing the age curve does not already say.
    ranked = sorted([g for g in groups if g["mean_peer_index"] is not None],
                    key=lambda g: g["mean_peer_index"])
    worst = ranked[0] if ranked else None
    best = ranked[-1] if ranked else None

    noise = int((labels == -1).sum())
    return {
        "available": True,
        "estate": st["estate"],
        "blocks": len(rows),
        "clusters": len(groups),
        "unclustered": noise,
        "groups": sorted(groups, key=lambda g: -g["blocks"]),
        "worst_group": worst,
        "best_group": best,
        "yield_gap_bunches_per_ha": (round(best["mean_bunches_per_ha"]
                                           - worst["mean_bunches_per_ha"], 1)
                                     if worst and best else None),
        "peer_index_gap": (round(best["mean_peer_index"] - worst["mean_peer_index"], 3)
                           if worst and best else None),
        "estate_mean": est_mean,
        "features": [{"key": k, "label": l, "provenance": p} for k, l, p in FEATURES],
        "feature_provenance": {
            "real": [k for k, _, p in FEATURES if p == "real"],
            "synthetic": [k for k, _, p in FEATURES if p == "synthetic"],
        },
        "method": {
            "algorithm": f"HDBSCAN, min_cluster_size={MIN_CLUSTER}",
            "scaling": "z-scored per feature across the estate",
            "labelling": (f"a cluster is named by axes more than {EXTREME_Z} "
                          "estate standard deviations from the mean"),
            "unclustered": ("HDBSCAN may assign no cluster. Those blocks are "
                            "reported as unclustered rather than forced into "
                            "the nearest group."),
        },
        "warning": ("Cluster names describe the data, not its causes. A group "
                    "that is low-yielding and high-fertiliser is showing the "
                    "agronomist's response to a problem, not its cause: weak "
                    "blocks are prescribed more. Read these as groups worth "
                    "visiting, not as diagnoses."),
        "provenance": ("mixed: canopy vigour, yield, palm age and elevation are "
                       "real measurements; nutrition, upkeep and the pest "
                       "census are synthetic."),
    }


def by_block(estate: str = "EC") -> dict:
    """{block_id: cluster} for colouring the map."""
    st = _state(estate)
    if not st or "error" in st:
        return {}
    return {r["block_id"]: r["_cluster"] for r in st["rows"]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    a = assess()
    if not a["available"]:
        raise SystemExit(a["reason"])
    print(f"\n{a['blocks']} blocks, {a['clusters']} clusters, "
          f"{a['unclustered']} unclustered")
    print(f"real axes: {a['feature_provenance']['real']}")
    print(f"synthetic axes: {a['feature_provenance']['synthetic']}\n")
    for g in a["groups"]:
        print(f"  cluster {g['cluster']}: {g['blocks']:3d} blocks, "
              f"{g['mean_bunches_per_ha']} bunches/ha  -  {g['label']}")
    if a["worst_group"]:
        w = a["worst_group"]
        print(f"\nworst: cluster {w['cluster']}, {w['blocks']} blocks, "
              f"{w['mean_bunches_per_ha']} bunches/ha")
        for t in w["traits"]:
            print(f"    {t['direction']:4s} {t['label']:22s} z={t['z']:+.2f}  ({t['provenance']})")
        print(f"\nyield gap best to worst: {a['yield_gap_bunches_per_ha']} bunches/ha")
