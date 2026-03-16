"""Signal formatter — validates and normalises the raw LLM output into a clean Signal object."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)

VALID_SIGNALS = {"Long", "Short", "Wait"}
VALID_CONFIDENCE = {"High", "Medium", "Low"}
VALID_HORIZONS = {"Intraday", "1-3 days", "This week"}


def format_signal(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Validate and normalise the raw LLM signal dict.
    Returns a clean Signal object with safe defaults for missing/invalid fields.
    """
    signal = raw.get("signal", "Wait")
    if signal not in VALID_SIGNALS:
        logger.warning(f"Invalid signal '{signal}' — defaulting to Wait")
        signal = "Wait"

    confidence = raw.get("confidence", "Low")
    if confidence not in VALID_CONFIDENCE:
        logger.warning(f"Invalid confidence '{confidence}' — defaulting to Low")
        confidence = "Low"

    horizon = raw.get("time_horizon", "Intraday")
    if horizon not in VALID_HORIZONS:
        horizon = "Intraday"

    key_levels = raw.get("key_levels") or {}
    if not isinstance(key_levels, dict):
        key_levels = {}

    return {
        "signal": signal,
        "confidence": confidence,
        "time_horizon": horizon,
        "rationale": str(raw.get("rationale", "")),
        "key_levels": {
            "support": key_levels.get("support"),
            "resistance": key_levels.get("resistance"),
        },
        "invalidation": str(raw.get("invalidation", "")),
        "risk_note": str(raw.get("risk_note", "")),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
