import structlog
from loguru import logger

slog = structlog.get_logger()


def go(n):
    slog.info("structlog", n=n)
    logger.warning("loguru {}", n)
