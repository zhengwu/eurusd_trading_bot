"""Structured logging: rotating file + console."""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


class _SafeRotatingFileHandler(RotatingFileHandler):
    """
    RotatingFileHandler with Windows-safe rotation.

    On Windows, renaming an open log file can raise PermissionError.
    This subclass silently skips the rotation if the rename fails, so the
    process keeps running and continues writing to the existing file.
    """

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except PermissionError:
            # Another process has the file open — skip rotation this cycle.
            pass


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — INFO and above
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file handler — DEBUG and above
    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    fh = _SafeRotatingFileHandler(
        log_dir / "eurusd_agent.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding="utf-8",
        delay=True,   # defer file open until first write (avoids lock at import)
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger
