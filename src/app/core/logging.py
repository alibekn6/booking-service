"""Structured JSON logs via structlog, with request context on every line.

Everything (our code, uvicorn, SQLAlchemy, alembic) goes through one stdlib handler
rendered by structlog, so each line is one JSON object and carries `request_id`
(and `user_id` once authenticated) from contextvars. That is what CloudWatch Logs
Insights / OpenSearch want later on AWS.
"""

import logging
import sys
from typing import Any

import orjson
import structlog
from structlog.typing import EventDict, Processor


def _orjson_dumps(obj: Any, default: Any = None) -> str:
    return orjson.dumps(obj, default=default).decode()


def _drop_color_message(_: Any, __: str, event_dict: EventDict) -> EventDict:
    # uvicorn duplicates the message with ANSI colors; noise in JSON.
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(level: str = "INFO", *, json: bool = True, access_log: bool = True) -> None:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        _drop_color_message,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    renderer: Processor = (
        structlog.processors.JSONRenderer(serializer=_orjson_dumps)
        if json
        else structlog.dev.ConsoleRenderer()
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Route uvicorn through our handler; its access log is replaced by our middleware,
    # which knows the request_id and the duration.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("app.access").disabled = not access_log
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
