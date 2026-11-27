"""Metric arithmetic (no models): M1 retrieval metrics and aggregation helpers."""
from __future__ import annotations

import math
import statistics


def recall_at_k(ranked: list[int], gold: list[int], k: int) -> float:
    return len(set(ranked[:k]) & set(gold)) / len(set(gold)) if gold else float("nan")


def hit_at_k(ranked: list[int], gold: list[int], k: int) -> float:
    return float(bool(set(ranked[:k]) & set(gold))) if gold else float("nan")


def reciprocal_rank(ranked: list[int], gold: list[int]) -> float:
    for rank, page in enumerate(ranked, 1):
        if page in gold:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked: list[int], gold: list[int], k: int) -> float:
    if not gold:
        return float("nan")
    dcg = sum(1.0 / math.log2(i + 2) for i, page in enumerate(ranked[:k]) if page in gold)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(set(gold)), k)))
    return dcg / ideal


def summarize(values: list[float | None]) -> dict:
    clean = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not clean:
        return {"mean": None, "std": None, "n": 0}
    return {"mean": round(statistics.fmean(clean), 4),
            "std": round(statistics.stdev(clean), 4) if len(clean) > 1 else 0.0, "n": len(clean)}


def by_group(rows: list[dict], key: str, metric: str) -> dict:
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row[key], []).append(row.get(metric))
    return {name: summarize(values) for name, values in sorted(groups.items())}
