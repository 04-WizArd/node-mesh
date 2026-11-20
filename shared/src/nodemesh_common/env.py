"""Small, dependency-free helpers for reading configuration from the environment."""

from __future__ import annotations

import os
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(RuntimeError):
    pass


_MISSING = object()


def load_env_file(path: Path) -> None:
    """Load ``KEY=VALUE`` lines into ``os.environ`` without overriding existing values.

    Supports comments, blank lines, ``export`` prefixes and single/double quotes.
    Real environment variables always win, so containers are unaffected.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_str(name: str, default: str | object = _MISSING) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        if default is _MISSING:
            raise ConfigError(f"environment variable {name} is required")
        return str(default)
    return value


def env_int(name: str, default: int | object = _MISSING, *, minimum: int | None = None) -> int:
    raw = env_str(name, default if default is _MISSING else str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def env_float(name: str, default: float | object = _MISSING) -> float:
    raw = env_str(name, default if default is _MISSING else str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean (true/false)")


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]
