"""A processed lecture: document + descriptions + sections + index, plus access to models by role."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import load_settings
from .describe import describe_document
from .ingest import ingest, lecture_dir, load_document
from .llm import FallbackLLM, client
from .retrieve import Retriever
from .structure import annotate, content_pages


class Lecture:
    def __init__(self, document: dict, settings: dict | None = None, llms: dict | None = None):
        self.document = document
        self.settings = settings or load_settings()
        self._llms = llms or {}  # role -> client override (tests, benchmark arms)
        self._retriever = None
        annotate(self.document)

    # ----- construction -----
    @classmethod
    def from_file(cls, path: str | Path, settings: dict | None = None, name: str | None = None,
                  describe: bool = True, progress=None, llms: dict | None = None) -> "Lecture":
        settings = settings or load_settings()
        if progress:
            progress("Reading the lecture file")
        document = ingest(path, settings, name)
        lecture = cls(document, settings, llms)
        if describe and settings["retrieval"].get("use_descriptions", True):
            lecture.describe(progress)
        return lecture

    @classmethod
    def load(cls, doc_id: str, settings: dict | None = None, llms: dict | None = None) -> "Lecture":
        return cls(load_document(doc_id), settings, llms)

    def describe(self, progress=None) -> dict:
        return describe_document(self.document, self.llm("describer"), progress)

    # ----- access -----
    @property
    def doc_id(self) -> str:
        return self.document["doc_id"]

    @property
    def name(self) -> str:
        return self.document["name"]

    @property
    def folder(self) -> Path:
        return lecture_dir(self.doc_id)

    def page(self, source_id: str) -> dict:
        return next(p for p in self.document["pages"] if p["source_id"] == source_id)

    def pages(self, source_ids: list[str]) -> list[dict]:
        wanted = set(source_ids)
        return [p for p in self.document["pages"] if p["source_id"] in wanted]

    def content_pages(self) -> list[dict]:
        return content_pages(self.document)

    def llm(self, role: str):
        if role in self._llms:
            return self._llms[role]
        models = self.settings["models"]
        if models.get(f"{role}_fallback"):  # used only when the main model's daily limit is reached
            return FallbackLLM(client(models[role]), client(models[f"{role}_fallback"]))
        return client(models[role])

    @property
    def retriever(self) -> Retriever:
        if self._retriever is None:
            self._retriever = Retriever(self.document, self.settings)
        return self._retriever

    def images_for(self, llm, slides: list[dict]) -> list[tuple[str, str]]:
        if not getattr(llm, "vision", False):
            return []
        informative = [s for s in slides if s.get("image") and (
            s.get("has_images") or any(v.get("informative") for v in (s.get("description") or {}).get("visual_elements", [])))]
        return [(s["source_id"], s["image"]) for s in informative[: self.settings.get("max_images", 4)]]

    # ----- records -----
    def save_output(self, kind: str, record: dict) -> Path:
        folder = self.folder / "outputs"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        path = folder / f"{kind}_{stamp}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return path
