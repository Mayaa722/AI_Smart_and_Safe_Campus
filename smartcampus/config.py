"""Paths, environment and settings. Everything configurable lives in config/*.json."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"
DATA_DIR = ROOT / "data"
RUNS_DIR = Path(os.environ.get("SMARTCAMPUS_RUNS", ROOT / "runs"))


def load_env(path: Path = ROOT / ".env") -> None:
    """Read KEY=VALUE lines from .env without overriding real environment variables."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(name.strip(), value)


load_env()


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_settings(overrides: dict | None = None, path: Path | None = None) -> dict:
    settings = json.loads((path or CONFIG_DIR / "settings.json").read_text(encoding="utf-8"))
    return _deep_merge(settings, overrides or {})


def load_models(path: Path | None = None) -> dict:
    models = json.loads((path or CONFIG_DIR / "models.json").read_text(encoding="utf-8"))
    return {name: spec for name, spec in models.items() if not name.startswith("_")}
