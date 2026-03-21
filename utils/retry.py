"""Exponential-backoff retry for transient API failures.

Usage:
    from utils.retry import call_with_retry
    message = call_with_retry(client.messages.create, **kwargs)
"""
from __future__ import annotations

import time
from typing import Any, Callable

from utils.logger import get_logger

logger = get_logger(__name__)

# Anthropic rate-limit / overload error names (no hard dependency on anthropic SDK)
_RETRYABLE_NAMES = {
    "RateLimitError",
    "APIStatusError",
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "OverloadedError",
}


def _is_retryable(exc: Exception) -> bool:
    return type(exc).__name__ in _RETRYABLE_NAMES


def call_with_retry(
    fn: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    **kwargs: Any,
) -> Any:
    """
    Call fn(*args, **kwargs) with exponential backoff on transient errors.

    Retries up to max_attempts times. Delays: 2s, 4s, 8s (base_delay * 2^attempt).
    Raises the last exception if all attempts fail.
    """
    last_exc: Exception | None = None

    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc):
                raise  # non-transient — fail immediately
            delay = base_delay * (2 ** attempt)
            logger.warning(
                f"API call failed ({type(exc).__name__}), "
                f"retry {attempt + 1}/{max_attempts} in {delay:.0f}s — {exc}"
            )
            time.sleep(delay)

    raise last_exc  # type: ignore[misc]
