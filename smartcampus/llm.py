"""One client for every model: Gemini (native REST) and OpenAI-compatible APIs
(Groq, Mistral, GitHub Models, Cerebras, OpenRouter).

Features: structured JSON output + schema validation, a persistent response cache (identical
requests never cost quota twice), a persistent per-model request ledger enforcing RPM/RPD/TPM,
bounded retries for temporary errors, and timing/usage records for the benchmark.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests
from jsonschema import ValidationError, validate

from .config import RUNS_DIR, load_models
from .text import restore_latex_in


class QuotaExceeded(RuntimeError):
    """Daily limit reached (locally or reported by the provider). Do not retry today."""


@dataclass
class LLMResponse:
    model: str
    provider: str
    parsed: dict | None = None
    text: str = ""
    error: str | None = None
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)
    seconds: float = 0.0
    waited_seconds: float = 0.0
    cached: bool = False
    request_hash: str = ""

    @property
    def ok(self) -> bool:
        return self.parsed is not None and self.error is None

    def to_dict(self) -> dict:
        return asdict(self)


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start > 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


OVERLOAD_DELAYS = (30.0, 60.0, 120.0)  # seconds between retries after a 5xx ("model overloaded")


def estimate_tokens(*texts: str, images: int = 0) -> int:
    return sum(len(t) for t in texts) // 4 + images * 1000


class Ledger:
    """Persistent request timestamps per model, so limits survive restarts."""
    _lock = threading.Lock()

    def __init__(self, path: Path, rpm: int, rpd: int, tpm: int):
        self.path, self.rpm, self.rpd, self.tpm = path, rpm, rpd, tpm

    def _load(self) -> list:
        cutoff = time.time() - 86400
        for _ in range(20):  # another process may be replacing the file right now
            if not self.path.exists():
                return []
            try:
                return [r for r in json.loads(self.path.read_text(encoding="utf-8")) if r[0] > cutoff]
            except (ValueError, OSError):
                time.sleep(0.05)
        return []

    def _write(self, rows: list) -> None:
        """Atomic replace, so a second process (e.g. two runs at once) never reads a half-written file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(rows), encoding="utf-8")
        for _ in range(20):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:  # Windows: the other process has it open for reading
                time.sleep(0.05)
        tmp.unlink(missing_ok=True)

    def release(self, stamp: float | None) -> None:
        """Take back a reservation the provider never served (e.g. 503 overloaded), so it does not
        count toward the local daily limit."""
        if stamp is None:
            return
        with self._lock:
            rows = self._load()
            kept = [r for r in rows if r[0] != stamp]
            if len(kept) != len(rows):
                self._write(kept)

    def reserve(self, tokens: int) -> float:
        """Block until a request fits the limits; return seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                rows, now = self._load(), time.time()
                if len(rows) >= self.rpd:
                    raise QuotaExceeded(f"Local daily request limit ({self.rpd}) reached for {self.path.stem}.")
                minute = [r for r in rows if now - r[0] < 60]
                used = sum(r[1] for r in minute)
                # a request larger than the whole per-minute budget is sent alone (otherwise it would wait forever)
                if len(minute) < self.rpm and (used + tokens <= self.tpm or not minute):
                    rows.append([now, tokens])
                    self._write(rows)
                    self.last_stamp = now
                    return waited
                delay = max(1.0, 60 - (now - min(r[0] for r in minute)) + 0.5) if minute else 1.0
            time.sleep(delay)
            waited += delay


class LLMClient:
    def __init__(self, name: str, spec: dict | None = None, cache: bool = True,
                 runs_dir: Path = RUNS_DIR, max_retries: int = 3, timeout: int = 180):
        self.name = name
        self.spec = spec or load_models()[name]
        self.cache = cache
        self.cache_dir = runs_dir / "cache" / "llm"
        self.ledger = Ledger(runs_dir / "ledger" / f"{name}.json",
                             self.spec.get("rpm", 10), self.spec.get("rpd", 500), self.spec.get("tpm", 10**9))
        self.max_retries = max_retries
        self.timeout = timeout

    @property
    def vision(self) -> bool:
        return bool(self.spec.get("vision"))

    def generate(self, system: str, user: str, schema: dict | None = None,
                 images: list[tuple[str, str]] | None = None, temperature: float = 0.0,
                 max_output_tokens: int | None = None, task: str = "") -> LLMResponse:
        images = images if self.vision else []
        images = images or []
        if "temperature" in self.spec:  # model-specific (e.g. Qwen repeats itself at temperature 0)
            temperature = self.spec["temperature"]
        max_out = min(max_output_tokens or self.spec.get("max_output_tokens", 4096),
                      self.spec.get("max_output_tokens", 4096))
        if schema and self.spec.get("schema_mode") == "json_object":
            user = user + "\n\nReturn JSON matching this JSON Schema:\n" + json.dumps(schema)
        image_hashes = [(sid, hashlib.sha256(Path(p).read_bytes()).hexdigest()) for sid, p in images]
        key_fields = {
            "model": self.spec["model"], "provider": self.spec["provider"], "system": system,
            "user": user, "schema": schema, "images": image_hashes, "temperature": temperature,
            "max_out": max_out, "schema_mode": self.spec.get("schema_mode")}
        if self.spec.get("extra_body"):  # e.g. reasoning effort changes the output
            key_fields["extra_body"] = self.spec["extra_body"]
        key = hashlib.sha256(json.dumps(key_fields, sort_keys=True).encode()).hexdigest()
        cache_path = self.cache_dir / f"{key}.json"
        if self.cache and cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            data["parsed"] = restore_latex_in(data.get("parsed"))  # also fixes replies cached before the fix
            return LLMResponse(**{**data, "cached": True})

        result = LLMResponse(model=self.spec["model"], provider=self.spec["provider"], request_hash=key)
        api_key = os.environ.get(self.spec["key_env"], "")
        if not api_key:
            result.error = f"Missing API key: set {self.spec['key_env']} in .env"
            return result
        tokens = estimate_tokens(system, user, images=len(images)) + max_out
        # Groq counts the reserved output in its per-minute limit: a request above it is refused (HTTP 413)
        if tokens - max_out > self.spec.get("max_input_tokens", 10**9) or tokens > self.spec.get("tpm", 10**9):
            result.error = f"Input too large for {self.name} (~{tokens - max_out} tokens)."
            return result

        start = time.perf_counter()
        format_retried = False
        for attempt in range(self.max_retries + 1):
            result.waited_seconds += self.ledger.reserve(tokens)
            try:
                if self.spec["provider"] == "gemini":
                    text, finish, usage = self._gemini(api_key, system, user, schema, images, temperature, max_out)
                else:
                    text, finish, usage = self._openai(api_key, system, user, schema, images, temperature, max_out)
                result.text, result.finish_reason, result.usage, result.error = text, finish, usage, None
                break
            except _FormatError as exc:  # provider rejected malformed JSON: retry once only
                result.error = str(exc)
                if format_retried or attempt == self.max_retries:
                    break
                format_retried = True
            except _Retryable as exc:
                result.error = str(exc)
                overloaded = exc.status is not None and exc.status >= 500
                if overloaded:  # never served: does not count toward the local daily limit
                    self.ledger.release(getattr(self.ledger, "last_stamp", None))
                if attempt == self.max_retries:
                    break
                # an overloaded provider ("high demand", 503) needs minutes, not seconds, to recover
                delay = (exc.retry_after or OVERLOAD_DELAYS[min(attempt, len(OVERLOAD_DELAYS) - 1)] if overloaded
                         else min(60.0, exc.retry_after or 5.0 * 2 ** attempt))
                time.sleep(delay)
                result.waited_seconds += delay
            except requests.RequestException as exc:
                result.error = f"{type(exc).__name__}"
                if attempt == self.max_retries:
                    break
                time.sleep(5.0 * 2 ** attempt)
            except _Fatal as exc:
                result.error = str(exc)
                break
        result.seconds = time.perf_counter() - start

        if result.error is None:
            if result.finish_reason not in (None, "STOP", "stop"):
                result.error = f"Incomplete output (finish reason: {result.finish_reason})"
            elif schema is not None:
                try:
                    result.parsed = restore_latex_in(parse_json(result.text))  # "\text" read as TAB+"ext" etc.
                    validate(result.parsed, schema)
                except (json.JSONDecodeError, ValidationError) as exc:
                    result.parsed = None
                    result.error = f"Invalid JSON output: {str(exc).splitlines()[0][:300]}"
            else:
                result.parsed = {"text": result.text}
        if result.ok and self.cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False), encoding="utf-8")
        return result

    # ----- providers -----
    def _gemini(self, api_key, system, user, schema, images, temperature, max_out):
        parts = [{"text": user}]
        for source_id, path in images:
            parts += [{"text": f"Image for slide [{source_id}]:"},
                      {"inlineData": {"mimeType": _mime(path), "data": _b64(path)}}]
        config = {"temperature": temperature, "maxOutputTokens": max_out}
        if schema is not None:
            config["responseMimeType"] = "application/json"
            if self.spec.get("schema_mode") == "json_schema":
                config["responseJsonSchema"] = schema
        payload = {"systemInstruction": {"parts": [{"text": system}]},
                   "contents": [{"role": "user", "parts": parts}], "generationConfig": config}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.spec['model']}:generateContent"
        response = requests.post(url, headers={"x-goog-api-key": api_key}, json=payload,
                                 timeout=(15, self.timeout))
        _raise_for_status(response, api_key)
        data = response.json()
        candidate = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", [])
                       if not p.get("thought"))
        return text, candidate.get("finishReason"), data.get("usageMetadata", {})

    def _openai(self, api_key, system, user, schema, images, temperature, max_out):
        content = [{"type": "text", "text": user}]
        for source_id, path in images:
            content += [{"type": "text", "text": f"Image for slide [{source_id}]:"},
                        {"type": "image_url", "image_url": {"url": f"data:{_mime(path)};base64,{_b64(path)}"}}]
        payload = {"model": self.spec["model"], "temperature": temperature, "max_tokens": max_out,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": content if images else user}]}
        if schema is not None:
            payload["response_format"] = (
                {"type": "json_schema", "json_schema": {"name": "output", "strict": True, "schema": schema}}
                if self.spec.get("schema_mode") == "json_schema" else {"type": "json_object"})
        payload.update(self.spec.get("extra_body", {}))
        response = requests.post(self.spec["base_url"].rstrip("/") + "/chat/completions",
                                 headers={"Authorization": f"Bearer {api_key}"}, json=payload,
                                 timeout=(15, self.timeout))
        _raise_for_status(response, api_key)
        data = response.json()
        choice = data["choices"][0]
        return choice["message"].get("content") or "", choice.get("finish_reason"), data.get("usage", {})


class _Retryable(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None, status: int | None = None):
        self.status = status
        super().__init__(message)
        self.retry_after = retry_after


class _Fatal(RuntimeError):
    pass


class _FormatError(RuntimeError):
    """The provider's own JSON-schema check rejected the output (Groq: json_validate_failed)."""


def _raise_for_status(response: requests.Response, api_key: str) -> None:
    if response.status_code == 200:
        if "json" not in response.headers.get("content-type", "json").lower():
            # e.g. a proxy or a retired endpoint answering "OK" in plain text: retrying will not help
            raise _Fatal(f"Provider returned a non-JSON response ({response.headers.get('content-type')}): "
                         f"{response.text[:120]!r}")
        return
    try:
        message = json.dumps(response.json().get("error", response.json()))[:400]
    except ValueError:
        message = response.text[:400]
    message = message.replace(api_key, "[REDACTED]")
    retry_after = None
    try:
        retry_after = float(response.headers.get("Retry-After", ""))
    except ValueError:
        pass
    # search the full body: Gemini names its daily quota ("...PerDay...") after the first 400 characters
    if response.status_code == 429 and re.search(r"per[ -]day|PerDay|RPD|TPD|daily", response.text, re.I):
        raise QuotaExceeded(f"Provider daily quota reached: {message}")
    if response.status_code == 400 and "json_validate_failed" in response.text:  # message may be truncated
        raise _FormatError(f"HTTP 400: {message}")
    if response.status_code in (429, 500, 502, 503, 504):
        raise _Retryable(f"HTTP {response.status_code}: {message}", retry_after, response.status_code)
    raise _Fatal(f"HTTP {response.status_code}: {message}")


def _mime(path: str) -> str:
    return "image/jpeg" if str(path).lower().endswith((".jpg", ".jpeg")) else "image/png"


def _b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


class FallbackLLM:
    """Main model, and a backup used only after the main model's daily limit is reached.
    Every response names the model that actually answered (LLMResponse.model)."""

    def __init__(self, primary, backup):
        self.primary, self.backup = primary, backup
        self.name = f"{getattr(primary, 'name', 'primary')}|{getattr(backup, 'name', 'backup')}"
        self.spec = primary.spec
        self.vision = primary.vision and backup.vision

    def generate(self, *args, **kwargs):
        # remembered on the (shared) primary client, so later calls in this run go straight to the backup
        if not getattr(self.primary, "quota_exhausted", None):
            try:
                return self.primary.generate(*args, **kwargs)
            except QuotaExceeded as exc:
                self.primary.quota_exhausted = str(exc)
        return self.backup.generate(*args, **kwargs)  # its own QuotaExceeded propagates


class FakeLLM:
    """Test double: returns responses from a function(system, user, schema, images) -> dict."""

    def __init__(self, respond, vision: bool = True, name: str = "fake"):
        self.respond, self.vision, self.name = respond, vision, name
        self.spec = {"model": name, "provider": "fake", "vision": vision}
        self.calls: list[dict] = []

    def generate(self, system, user, schema=None, images=None, temperature=0.0,
                 max_output_tokens=None, task=""):
        self.calls.append({"system": system, "user": user, "schema": schema,
                           "images": images or [], "task": task})
        parsed = self.respond(system, user, schema, images or [])
        if schema is not None:
            validate(parsed, schema)
        return LLMResponse(model=self.name, provider="fake", parsed=parsed, text=json.dumps(parsed))


_CLIENTS: dict[str, LLMClient] = {}


def client(name: str) -> LLMClient:
    if name not in _CLIENTS:
        _CLIENTS[name] = LLMClient(name)
    return _CLIENTS[name]
