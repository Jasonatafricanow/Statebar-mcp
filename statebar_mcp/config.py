"""Deployment configuration (env vars + optional JSON file).

The extractor model is a deployment config item (派单总纲 §8): nothing here
binds a concrete model. Recognized environment variables:

    DSH_USER_STATE_DB              SQLite path (default: ~/.statebar-mcp/user_state.db)
    DSH_USER_STATE_LLM_ENABLED     "1"/"true" — explicit opt-in to send user
                                   message text to the configured LLM endpoint
                                   (privacy boundary; see README §隐私)
    DSH_USER_STATE_LLM_BASE_URL    OpenAI-compatible base URL (persistent extractor)
    DSH_USER_STATE_LLM_API_KEY     API key
    DSH_USER_STATE_LLM_MODEL       model name
    DSH_USER_STATE_LLM_MOCK        "1"/"true" — deterministic mock extractor
                                   (local only, no network egress)
    DSH_USER_STATE_SERVE_HOST      REST bind host (default 127.0.0.1)
    DSH_USER_STATE_SERVE_PORT      REST bind port (default 8765)
    DSH_USER_STATE_SERVE_TOKEN     Bearer token; when set, EVERY endpoint
                                   (incl. /v1/health) requires Authorization
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

_TRUE_VALUES = {"1", "true", "yes", "on"}


def default_home() -> Path:
    env = os.environ.get("DSH_USER_STATE_HOME")
    if env:
        return Path(env)
    return Path.home() / ".statebar-mcp"


def default_db_path() -> Path:
    env = os.environ.get("DSH_USER_STATE_DB")
    if env:
        return Path(env)
    return default_home() / "user_state.db"


@dataclass
class ExtractorConfig:
    enabled: bool = False  # explicit opt-in: send raw user text to an external LLM
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 30.0
    use_mock: bool = False


@dataclass
class ServeConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str = ""  # empty = no auth (localhost default); required for
    # non-loopback binds (fail-closed, enforced by the CLI)
    max_body_bytes: int = 64 * 1024


@dataclass
class Config:
    db_path: Path = field(default_factory=default_db_path)
    extractor: ExtractorConfig = field(default_factory=ExtractorConfig)
    serve: ServeConfig = field(default_factory=ServeConfig)

    def ensure_dirs(self) -> None:
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _read_json_config() -> Dict[str, Any]:
    path = default_home() / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_config() -> Config:
    """Load from JSON file first, then env vars (env wins)."""
    jc = _read_json_config()
    ext = jc.get("extractor", {}) if isinstance(jc.get("extractor"), dict) else {}
    serve = jc.get("serve", {}) if isinstance(jc.get("serve"), dict) else {}

    db_path = Path(_env("DSH_USER_STATE_DB") or jc.get("db_path") or default_db_path())
    extractor = ExtractorConfig(
        enabled=_env_bool("DSH_USER_STATE_LLM_ENABLED", bool(ext.get("enabled", False))),
        base_url=_env("DSH_USER_STATE_LLM_BASE_URL") or ext.get("base_url", ""),
        api_key=_env("DSH_USER_STATE_LLM_API_KEY") or ext.get("api_key", ""),
        model=_env("DSH_USER_STATE_LLM_MODEL") or ext.get("model", ""),
        timeout=float(_env("DSH_USER_STATE_LLM_TIMEOUT") or ext.get("timeout", 30.0)),
        use_mock=_env_bool("DSH_USER_STATE_LLM_MOCK", bool(ext.get("use_mock", False))),
    )
    serve_cfg = ServeConfig(
        host=_env("DSH_USER_STATE_SERVE_HOST") or serve.get("host", "127.0.0.1"),
        port=int(_env("DSH_USER_STATE_SERVE_PORT") or serve.get("port", 8765)),
        auth_token=_env("DSH_USER_STATE_SERVE_TOKEN") or str(serve.get("auth_token", "") or ""),
        max_body_bytes=int(
            _env("DSH_USER_STATE_MAX_BODY_BYTES") or serve.get("max_body_bytes", 64 * 1024)
        ),
    )
    cfg = Config(db_path=db_path, extractor=extractor, serve=serve_cfg)
    cfg.ensure_dirs()
    return cfg


def is_loopback_host(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def build_persistent_extractor(cfg: Config):
    """Construct the configured persistent extractor (or the no-op one).

    Privacy rule: an external LLM backend is used ONLY when the operator
    explicitly opted in (``enabled``) AND an endpoint is configured.
    The mock extractor is always allowed — it is local and deterministic.
    """
    from .core.extractor.persistent import MockExtractor, OpenAICompatExtractor

    ext = cfg.extractor
    if ext.use_mock:
        return MockExtractor()
    if ext.enabled and ext.base_url and ext.model:
        return OpenAICompatExtractor(
            base_url=ext.base_url,
            api_key=ext.api_key,
            model=ext.model,
            timeout=ext.timeout,
        )
    from .core.extractor.persistent import PersistentExtractor

    return PersistentExtractor()  # unavailable → async path disabled gracefully
