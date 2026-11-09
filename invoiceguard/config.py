"""Settings read from environment variables. No secrets are ever written to disk by this module."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


class ConfigError(ValueError):
    """Raised when an environment variable has an unusable value."""


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 1) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


def _opt(env: Mapping[str, str], name: str) -> Optional[str]:
    value = env.get(name)
    return value.strip() if value and value.strip() else None


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    anthropic_api_key: Optional[str] = None
    anthropic_base_url: str = "https://api.anthropic.com"
    model: str = "claude-sonnet-5-5"
    admin_token: Optional[str] = None
    max_upload_bytes: int = 10 * 1024 * 1024
    rate_limit_per_minute: int = 60
    public_base_url: Optional[str] = None
    trust_proxy: bool = False
    max_concurrent_reads: int = 4
    auth_failures_per_minute: int = 20
    reader: str = "claude"
    offsite_backup_dir: Optional[Path] = None
    monitor_url: Optional[str] = None

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        base = (_opt(env, "ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
        local = base.startswith(("http://localhost", "http://127.0.0.1"))
        if not (base.startswith("https://") or local):
            raise ConfigError("ANTHROPIC_BASE_URL must start with https://")
        admin = _opt(env, "INVOICEGUARD_ADMIN_TOKEN")
        if admin is not None and len(admin) < 16:
            raise ConfigError("INVOICEGUARD_ADMIN_TOKEN must be at least 16 characters")
        public = _opt(env, "INVOICEGUARD_PUBLIC_URL")
        reader = (_opt(env, "INVOICEGUARD_READER") or "claude").lower()
        if reader not in {"claude", "local"}:
            raise ConfigError("INVOICEGUARD_READER must be 'claude' or 'local'")
        monitor = _opt(env, "INVOICEGUARD_MONITOR_URL")
        if monitor is not None and not monitor.startswith(("http://", "https://")):
            raise ConfigError("INVOICEGUARD_MONITOR_URL must start with http:// or https://")
        offsite = _opt(env, "INVOICEGUARD_OFFSITE_BACKUP_DIR")
        return cls(
            data_dir=Path(_opt(env, "INVOICEGUARD_DATA_DIR") or "./data").resolve(),
            anthropic_api_key=_opt(env, "ANTHROPIC_API_KEY"),
            anthropic_base_url=base,
            model=_opt(env, "INVOICEGUARD_MODEL") or "claude-sonnet-5-5",
            admin_token=admin,
            max_upload_bytes=_int(env, "INVOICEGUARD_MAX_UPLOAD_MB", 10) * 1024 * 1024,
            rate_limit_per_minute=_int(env, "INVOICEGUARD_RATE_LIMIT_PER_MIN", 60),
            public_base_url=public.rstrip("/") if public else None,
            trust_proxy=(_opt(env, "INVOICEGUARD_TRUST_PROXY") or "").lower() in ("1", "true", "yes"),
            max_concurrent_reads=_int(env, "INVOICEGUARD_MAX_CONCURRENT_READS", 4),
            auth_failures_per_minute=_int(env, "INVOICEGUARD_AUTH_FAILURES_PER_MIN", 20),
            reader=reader,
            offsite_backup_dir=Path(offsite).resolve() if offsite else None,
            monitor_url=monitor,
        )
