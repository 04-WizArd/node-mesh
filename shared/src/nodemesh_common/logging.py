"""One-line JSON log formatter used by both Django projects.

Fields passed through ``logger.info("...", extra={...})`` are emitted as
top-level keys, which keeps logs greppable and machine-parseable without an
extra dependency.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys() | {"message", "asctime"}
)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str = "") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if self.service:
            payload["service"] = self.service
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)
