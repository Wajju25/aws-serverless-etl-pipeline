"""Structured JSON logging for AWS Lambda.

Every log line is a single JSON object, which makes the output directly
queryable in CloudWatch Logs Insights, for example::

    fields @timestamp, level, message, key
    | filter level = "ERROR"

Any keyword passed through ``extra={...}`` on a logging call is merged into
the emitted JSON document, so call sites can attach request-scoped context
without string formatting:

    logger.info("object processed", extra={"key": key, "records": count})
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

#: Attributes that ``logging.LogRecord`` populates on its own. Anything else
#: found on a record was supplied via ``extra`` and belongs in the payload.
_RESERVED_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def _jsonable(value: Any) -> Any:
    """Return *value* if it serializes to JSON, otherwise its ``repr``."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value


class JsonFormatter(logging.Formatter):
    """Render each log record as one line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = _jsonable(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    """Return a logger that emits structured JSON to stdout.

    The log level is driven by the ``LOG_LEVEL`` environment variable and
    defaults to ``INFO``. Calling this repeatedly with the same name is safe;
    the JSON handler is attached only once.
    """
    logger = logging.getLogger(name)
    if not any(isinstance(handler.formatter, JsonFormatter) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    return logger
