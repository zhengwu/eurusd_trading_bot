"""MT5 connector — connect/disconnect/status.

MT5 terminal must already be running on the machine (auto-connect mode).
Implemented using patterns from the user's trend_ma_EA.py.
"""
from __future__ import annotations

import MetaTrader5 as mt5

import config
from utils.logger import get_logger

logger = get_logger(__name__)


def connect() -> bool:
    """
    Connect to the already-running MT5 terminal.
    Returns True on success.
    """
    if not mt5.initialize():
        err = mt5.last_error()
        logger.error(f"MT5 initialize() failed: {err}")
        return False

    # Ensure the symbol is visible in Market Watch
    symbol_info = mt5.symbol_info(config.MT5_SYMBOL)
    if symbol_info is None:
        logger.error(f"Symbol {config.MT5_SYMBOL} not found in MT5")
        mt5.shutdown()
        return False

    if not symbol_info.visible:
        if not mt5.symbol_select(config.MT5_SYMBOL, True):
            logger.error(f"Failed to select {config.MT5_SYMBOL} in Market Watch")
            mt5.shutdown()
            return False

    acct = mt5.account_info()
    server = acct.server if acct else "unknown"
    login = acct.login if acct else "unknown"
    logger.info(f"MT5 connected — account={login} server={server} symbol={config.MT5_SYMBOL}")
    return True


def disconnect() -> None:
    """Disconnect from MT5 terminal."""
    mt5.shutdown()
    logger.info("MT5 disconnected")


def is_connected() -> bool:
    """Check if MT5 terminal connection is active."""
    try:
        info = mt5.terminal_info()
        return info is not None and info.connected
    except Exception:
        return False


def get_timeframe(tf_str: str) -> int:
    """Map timeframe string to MT5 timeframe constant."""
    mapping = {
        "M1":  mt5.TIMEFRAME_M1,
        "M5":  mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1":  mt5.TIMEFRAME_H1,
        "H4":  mt5.TIMEFRAME_H4,
        "D1":  mt5.TIMEFRAME_D1,
        "W1":  mt5.TIMEFRAME_W1,
        "MN1": mt5.TIMEFRAME_MN1,
    }
    tf = mapping.get(tf_str.upper())
    if tf is None:
        logger.warning(f"Unknown timeframe '{tf_str}' — defaulting to M15")
        return mt5.TIMEFRAME_M15
    return tf
