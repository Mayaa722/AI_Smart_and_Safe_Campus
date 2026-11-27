"""Step 6: dense embeddings in ChromaDB + BM25 (stopwords removed, math symbols kept)."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from functools import lru_cache
from pathlib import Path

import numpy as np

from .chunking import build_chunks
from .ingest import lecture_dir
from .text import tokens


class BM25:
    def __init__(self, texts: list[str], k1: float = 1.5, b: float = 0.75):
        self.docs = [Counter(tokens(t)) for t in texts]
        self.lengths = np.array([sum(d.values()) for d in self.docs], dtype=float)
        self.avg = float(self.lengths.mean()) if len(self.docs) else 1.0
        self.df = Counter(term for d in self.docs for term in d)
        self.k1, self.b, self.n = k1, b, len(self.docs)

    def scores(self, query: str) -> np.ndarray:
        result = np.zeros(self.n)
        for term in set(tokens(query)):
            df = self.df.get(term, 0)
            if not df:
                continue
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            freq = np.array([d.get(term, 0) for d in self.docs], dtype=float)
            result += idf * freq * (self.k1 + 1) / (freq + self.k1 * (1 - self.b + self.b * self.lengths / max(self.avg, 1)))
        return result


class HashEmbedder:
    """Deterministic stand-in for tests and dry runs (no download). Not for real retrieval."""
    name = "hash"

    def encode(self, texts: list[str]) -> np.ndarray:
        vectors = np.zeros((len(texts), 256))
        for i, text in enumerate(texts):
            for token in tokens(text):
                vectors[i, int(hashlib.md5(token.encode()).hexdigest(), 16) % 256] += 1
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.where(norms == 0, 1, norms)


class STEmbedder:
    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer
        self.name = model_name
        self.model = SentenceTransformer(model_name, device="cpu")

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.model.encode(texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False))


@lru_cache(maxsize=4)
def embedder(model_name: str):
    return HashEmbedder() if model_name == "hash" else STEmbedder(model_name)


@lru_cache(maxsize=2)
def reranker(model_name: str):
    from sentence_transformers import CrossEncoder
    return CrossEncoder(model_name, device="cpu")


def index_key(document: dict, settings: dict) -> str:
    r = settings["retrieval"]
    relevant = {"embedding": settings["embedding_model"], "chunk_chars": settings["chunk_chars"],
                "profile": settings.get("subject_profile"), "overlap": settings.get("chunk_overlap"),
                "chunking": r.get("chunking"), "descriptions": r.get("use_descriptions"),
                "header": r.get("use_context_header"),
                "pages": hashlib.sha256(json.dumps([[p.get("text"), p.get("description"), p.get("section"),
                                                     p.get("tables")] for p in document["pages"]],
                                                   sort_keys=True).encode()).hexdigest()}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]


class LectureIndex:
    def __init__(self, document: dict, settings: dict):
        self.document, self.settings = document, settings
        self.key = index_key(document, settings)
        self.folder: Path = lecture_dir(document["doc_id"]) / "index" / self.key
        chunk_file = self.folder / "chunks.json"
        if chunk_file.exists():
            self.chunks = json.loads(chunk_file.read_text(encoding="utf-8"))
        else:
            self.chunks = build_chunks(document, settings)
            self.folder.mkdir(parents=True, exist_ok=True)
            chunk_file.write_text(json.dumps(self.chunks, ensure_ascii=False), encoding="utf-8")
        self.by_id = {c["chunk_id"]: c for c in self.chunks}
        self.bm25 = BM25([c["bm25_text"] for c in self.chunks])
        self._collection = None

    @property
    def collection(self):
        if self._collection is None:
            import chromadb
            from chromadb.config import Settings
            client = chromadb.PersistentClient(path=str(self.folder / "chroma"),
                                               settings=Settings(anonymized_telemetry=False))
            collection = client.get_or_create_collection("chunks", metadata={"hnsw:space": "cosine"})
            if collection.count() != len(self.chunks):
                if collection.count():
                    client.delete_collection("chunks")
                    collection = client.get_or_create_collection("chunks", metadata={"hnsw:space": "cosine"})
                vectors = embedder(self.settings["embedding_model"]).encode([c["embed_text"] for c in self.chunks])
                for start in range(0, len(self.chunks), 500):
                    part = self.chunks[start:start + 500]
                    collection.add(ids=[c["chunk_id"] for c in part],
                                   embeddings=[v.tolist() for v in vectors[start:start + 500]],
                                   documents=[c["embed_text"] for c in part],
                                   metadatas=[{"source_id": c["source_id"], "page": c["page"], "kind": c["kind"]} for c in part])
            self._collection = collection
        return self._collection

    def dense(self, query: str, n: int) -> list[tuple[str, float]]:
        if not self.chunks:
            return []
        vector = embedder(self.settings["embedding_model"]).encode([query])[0]
        result = self.collection.query(query_embeddings=[vector.tolist()], n_results=min(n, len(self.chunks)))
        return [(cid, 1.0 - float(dist)) for cid, dist in zip(result["ids"][0], result["distances"][0])]

    def lexical(self, query: str, n: int) -> list[tuple[str, float]]:
        scores = self.bm25.scores(query)
        order = np.argsort(-scores)[:n]
        return [(self.chunks[i]["chunk_id"], float(scores[i])) for i in order if scores[i] > 0]
