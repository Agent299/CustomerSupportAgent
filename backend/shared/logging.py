import logging

import structlog


def configure_logging(level: str = "INFO", *, json: bool = True) -> None:
    """JSON logs in deployed envs, readable console logs locally."""
    logging.basicConfig(format="%(message)s", level=level.upper())
    # Per-request / consumer-group chatter drowns the ingestion logs.
    for noisy in ("httpx", "aiokafka", "opensearch"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )
