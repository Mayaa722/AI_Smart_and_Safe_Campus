"""Step 9-10: hybrid retrieval (dense + BM25, RRF) -> reranker -> parent slides + neighbours
-> admin filter -> evidence gate. Every switch is in settings.retrieval so ablations are config-only."""
from __future__ import annotations

import math
import re
import time

from .index import LectureIndex, reranker
from .structure import NON_CONTENT

META_QUESTION = re.compile(r"\b(author|lecturer|instructor|credits?|outline|agenda|overview|title|contents)\b", re.I)


def rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


class Retriever:
    def __init__(self, document: dict, settings: dict, index: LectureIndex | None = None):
        self.document, self.settings = document, settings
        self.r = settings["retrieval"]
        self.index = index or LectureIndex(document, settings)
        self.pages = {p["source_id"]: p for p in document["pages"]}

    def search(self, query: str, top_k: int | None = None) -> dict:
        start = time.perf_counter()
        r, top_k = self.r, top_k or self.r["top_k"]
        n = r.get("candidates", 30)
        dense = self.index.dense(query, n) if r.get("use_dense", True) else []
        lexical = self.index.lexical(query, n) if r.get("use_bm25", True) else []
        fused = rrf([[c for c, _ in dense], [c for c, _ in lexical]], r.get("rrf_k", 60))
        candidates = sorted(fused, key=lambda c: -fused[c])[:n]
        dense_score = dict(dense)

        rerank_score: dict[str, float] = {}
        if r.get("use_reranker", True) and candidates:
            model = reranker(self.settings["reranker_model"])
            raw = model.predict([(query, self.index.by_id[c]["embed_text"]) for c in candidates])
            rerank_score = {c: _prob(float(s)) for c, s in zip(candidates, raw)}
            candidates = sorted(candidates, key=lambda c: -rerank_score[c])

        def score(c):
            return rerank_score.get(c, fused[c])

        allow_meta = bool(META_QUESTION.search(query))
        eligible = [c for c in candidates
                    if allow_meta or self.pages[self.index.by_id[c]["source_id"]].get("slide_type", "content") not in NON_CONTENT]

        if r.get("chunking") == "flat":  # paper baseline: top-K chunks, then their pages
            chosen_chunks = eligible[:top_k]
            core = list(dict.fromkeys(self.index.by_id[c]["source_id"] for c in chosen_chunks))
        else:
            best: dict[str, float] = {}
            for c in eligible:
                sid = self.index.by_id[c]["source_id"]
                best[sid] = max(best.get(sid, -1e9), score(c))
            core = sorted(best, key=lambda s: -best[s])[:top_k]

        page_scores = {}
        for c in candidates:
            sid = self.index.by_id[c]["source_id"]
            page_scores[sid] = max(page_scores.get(sid, -1e9), score(c))

        selected = [{"source_id": s, "page": self.pages[s]["page"], "score": round(page_scores[s], 4),
                     "reason": "retrieved"} for s in core]
        if r.get("neighbours", 0) and r.get("chunking") != "flat":
            selected += self._neighbours(core, r["neighbours"])
        selected = selected[: r.get("max_evidence_slides", 12)]

        top_dense = max((dense_score.get(c, 0.0) for c in candidates), default=0.0)
        top_rerank = max(rerank_score.values(), default=None)
        abstain, why = False, ""
        if r.get("gate_enabled", True) and candidates:
            if top_rerank is not None and top_rerank < r.get("gate_min_reranker_score", 0.05):
                abstain, why = True, f"best reranker score {top_rerank:.3f} below threshold"
            elif top_rerank is None and dense and top_dense < r.get("gate_min_dense_score", 0.35):
                abstain, why = True, f"best dense similarity {top_dense:.3f} below threshold"
        if not candidates:
            abstain, why = True, "no matching chunks"
        return {"query": query, "selected": selected, "abstain": abstain, "abstain_reason": why,
                "top_dense": round(top_dense, 4), "top_rerank": None if top_rerank is None else round(top_rerank, 4),
                "ranked_pages": sorted(page_scores, key=lambda s: -page_scores[s]),
                "seconds": round(time.perf_counter() - start, 3)}

    def _neighbours(self, core: list[str], width: int) -> list[dict]:
        extra, chosen = [], set(core)
        for sid in core:
            page = self.pages[sid]
            for offset in range(1, width + 1):
                for number in (page["page"] - offset, page["page"] + offset):
                    other = self.document["pages"][number - 1] if 1 <= number <= len(self.document["pages"]) else None
                    if (other and other["source_id"] not in chosen and other.get("section") == page.get("section")
                            and other.get("slide_type", "content") not in NON_CONTENT):
                        chosen.add(other["source_id"])
                        extra.append({"source_id": other["source_id"], "page": number, "score": None,
                                      "reason": f"neighbour of slide {page['page']}"})
        return extra

    def search_many(self, queries: list[str]) -> dict:
        """Several sub-questions: each keeps its best slides inside the total cap."""
        if len(queries) == 1:
            return self.search(queries[0])
        results = [self.search(q) for q in queries]
        cap, minimum = self.r.get("max_evidence_slides", 12), self.r.get("min_slides_per_question", 2)
        chosen: dict[str, dict] = {}
        for result in results:
            for item in [s for s in result["selected"] if s["reason"] == "retrieved"][:minimum]:
                chosen.setdefault(item["source_id"], item)
        pool = sorted((s for res in results for s in res["selected"]),
                      key=lambda s: (s["reason"] != "retrieved", -(s["score"] or 0)))
        for item in pool:
            if len(chosen) >= cap:
                break
            chosen.setdefault(item["source_id"], item)
        return {"query": queries, "selected": list(chosen.values())[:cap],
                "abstain": all(r["abstain"] for r in results),
                "abstain_reason": "; ".join(r["abstain_reason"] for r in results if r["abstain"]),
                "per_question": results, "seconds": round(sum(r["seconds"] for r in results), 3)}


def _prob(score: float) -> float:
    """CrossEncoder.predict may return logits or probabilities depending on the model; map to 0-1."""
    return score if 0.0 <= score <= 1.0 else 1 / (1 + math.exp(-score))
