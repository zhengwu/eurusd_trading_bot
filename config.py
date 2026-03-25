"""All tuneable parameters. No magic numbers in module code."""
from pathlib import Path

# ── Triage ────────────────────────────────────────────────────────────────────
TRIAGE_SCORE_THRESHOLD = 6       # Score >= this triggers full analysis
COOLDOWN_MINUTES = 45            # Minutes to suppress re-trigger after escalation
SCAN_INTERVAL_MINUTES = 30       # How often Tier 1 runs

# ── Models ────────────────────────────────────────────────────────────────────
TRIAGE_MODEL = "claude-haiku-4-5-20251001"
ANALYSIS_MODEL = "claude-sonnet-4-6"

# ── Regime Engine ──────────────────────────────────────────────────────────────
USE_REGIME_ANALYSIS = True   # Inject regime summary into LLM context window

# Central bank rate cycles — update after each meeting or major policy speech.
#   stance   : "Hiking" | "Pausing" | "Cutting"
#   guidance : Latest forward guidance in one sentence
#   expected : Market consensus for next 12 months
#   updated  : Date last reviewed — agent warns if stale > 14 days
RATE_CYCLES: dict[str, dict] = {
    "Fed": {
        "stance":   "Pausing",
        "guidance": "No rush to cut - watching inflation; tariff uncertainty clouds outlook",
        "expected": "1-2 cuts in 2025 (CME FedWatch ~55%)",
        "updated":  "2026-03-25",
    },
    "ECB": {
        "stance":   "Cutting",
        "guidance": "Gradual cuts continuing - inflation near 2% target, growth weak",
        "expected": "3-4 more cuts in 2025",
        "updated":  "2026-03-25",
    },
    "BOE": {
        "stance":   "Cutting",
        "guidance": "Cautious pace - sticky services inflation limits speed of cuts",
        "expected": "2-3 cuts in 2025",
        "updated":  "2026-03-25",
    },
    "BOJ": {
        "stance":   "Hiking",
        "guidance": "Gradual hikes - watching wage growth and FX pass-through to CPI",
        "expected": "1-2 hikes in 2025",
        "updated":  "2026-03-25",
    },
}

# ── Context window ────────────────────────────────────────────────────────────
CONTEXT_DAYS_SUMMARY = 7         # Days of summary.md to include
CONTEXT_DAYS_PRICES = 30         # Days of price trend to include
CONTEXT_MAX_TOKENS = 10000       # Hard cap — price agent now includes M15 bars + daily table

# ── Market hours (UTC) — scanner only runs during these hours on weekdays ─────
MARKET_HOURS_START = 7           # 07:00 UTC
MARKET_HOURS_END = 23            # 23:00 UTC (7pm EDT / 6pm EST)

# ── Weekend schedule (EST) ────────────────────────────────────────────────────
# Forex market closes Friday 5 PM EST, reopens Sunday 5 PM EST.
# The scanner pauses during this window.
MARKET_CLOSE_HOUR_EST = 17       # Friday: stop scanning at 5 PM EST
MARKET_OPEN_HOUR_EST = 17        # Sunday: resume scanning at 5 PM EST

# ── Daily collector ───────────────────────────────────────────────────────────
DAILY_COLLECTION_TIME_UTC = "22:00"

# ── Notifications ─────────────────────────────────────────────────────────────
NOTIFICATION_CHANNELS = ["print", "slack"]   # console + Slack (email available as option)
NOTIFICATION_EMAIL_TO = "zhengwu5650@gmail.com"

# ── Data ──────────────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent / "data"

# ── News fetch ────────────────────────────────────────────────────────────────
NEWS_WINDOW_DAYS = 1             # How many days back to fetch headlines
NEWS_PER_PROVIDER_MAX_ITEMS = 20 # Max items per provider per scan

# ── MT5 ───────────────────────────────────────────────────────────────────────
MT5_TIMEFRAME = "M15"
MT5_MAGIC_NUMBER = 20260314      # Identifies orders placed by this agent

# ── Multi-pair configuration ──────────────────────────────────────────────────
# Each pair defines its pip size, pip value per standard lot (USD account),
# yfinance daily ticker, price decimal places, and correlated assets.
#
# pip_value_per_lot notes:
#   USD-quote pairs (EURUSD, GBPUSD, AUDUSD): $10 per pip per standard lot
#   USD-base pairs (USDJPY, USDCAD):          pip_value = 100,000 * pip / rate
#                                              For USDJPY ~150: ≈ $6.7 (approximate)
#
PAIRS: dict[str, dict] = {
    "EURUSD": {
        "display":           "EUR/USD",
        "yf_ticker":         "EURUSD=X",
        "pip":               0.0001,
        "pip_value_per_lot": 10.0,
        "price_decimals":    5,
        "correlations": [
            ("DXY",    "DX-Y.NYB",   "",  "Inverse — DXY up = EUR/USD down"),
            ("Gold",   "GC=F",   "",  "Risk-on proxy — Gold up = mild EUR support"),
            ("US10Y",  "^TNX",   "%", "Yield — rising = USD strength pressure"),
            ("VIX",    "^VIX",   "",  "Fear index — VIX up = risk-off USD bid"),
            ("S&P500", "^GSPC",  "",  "Risk appetite — S&P up = mild risk-on"),
        ],
    },
    "GBPUSD": {
        "display":           "GBP/USD",
        "yf_ticker":         "GBPUSD=X",
        "pip":               0.0001,
        "pip_value_per_lot": 10.0,
        "price_decimals":    5,
        "correlations": [
            ("DXY",    "DX-Y.NYB",    "",  "Inverse — DXY up = GBP/USD down"),
            ("FTSE",   "^FTSE",   "",  "UK equities — FTSE up = GBP support"),
            ("EURGBP", "EURGBP=X","",  "EUR/GBP cross — rising = GBP weakness"),
            ("US10Y",  "^TNX",    "%", "Yield — rising = USD strength"),
            ("VIX",    "^VIX",    "",  "Fear index — VIX up = risk-off"),
        ],
    },
    "USDJPY": {
        "display":           "USD/JPY",
        "yf_ticker":         "USDJPY=X",
        "pip":               0.01,
        "pip_value_per_lot": 6.7,    # ~100,000 * 0.01 / 150 — update if rate moves far
        "price_decimals":    3,
        "correlations": [
            ("Nikkei", "^N225",  "",  "Japan equities — Nikkei up = JPY weakness"),
            ("US10Y",  "^TNX",   "%", "Yield — rising = USD/JPY up (carry trade)"),
            ("VIX",    "^VIX",   "",  "Fear index — VIX up = JPY safe-haven bid"),
            ("DXY",    "DX-Y.NYB",   "",  "USD index — DXY up = USD/JPY up"),
            ("Gold",   "GC=F",   "",  "Safe haven — Gold up = mixed JPY signal"),
        ],
    },
    "AUDUSD": {
        "display":           "AUD/USD",
        "yf_ticker":         "AUDUSD=X",
        "pip":               0.0001,
        "pip_value_per_lot": 10.0,
        "price_decimals":    5,
        "correlations": [
            ("Iron Ore", "VALE",  "",  "Iron ore proxy — Vale up = AUD support"),
            ("Gold",     "GC=F",  "",  "Commodity — Gold up = AUD support"),
            ("China",    "MCHI",  "",  "China ETF — MCHI up = AUD support"),
            ("DXY",      "DX-Y.NYB",  "",  "Inverse — DXY up = AUD/USD down"),
            ("VIX",      "^VIX",  "",  "Fear index — VIX up = risk-off AUD sold"),
        ],
    },
}

# Active pairs to scan. Edit this list to enable/disable pairs.
ACTIVE_PAIRS: list[str] = ["EURUSD", "GBPUSD", "USDJPY"]

# Convenience alias — primary pair (first in active list)
MT5_SYMBOL: str = ACTIVE_PAIRS[0]


def get_pair(symbol: str) -> dict:
    """Return pair metadata dict, raising KeyError if symbol not configured."""
    if symbol not in PAIRS:
        raise KeyError(f"Pair {symbol!r} not in config.PAIRS — add it first")
    return PAIRS[symbol]


# ── Job 2 — Position Monitor ──────────────────────────────────────────────────
JOB2_CHECK_INTERVAL_MINUTES = 30  # How often Job 2 checks open positions

# ── Job 3 — Trade Executor ────────────────────────────────────────────────────
JOB3_RISK_PCT = 1.0              # % of account equity to risk per trade
JOB3_MIN_LOT = 0.01              # Minimum lot size
JOB3_MAX_LOT = 1.0               # Maximum lot size
JOB3_DEFAULT_SL_PIPS = 30        # Fallback SL if key_levels missing
JOB3_DEFAULT_TP_PIPS = 60        # Fallback TP if key_levels missing
JOB3_SIGNAL_EXPIRY_MINUTES = 60  # Signal auto-expires if not approved in time

# ── Price assets to track (legacy — superseded by PAIRS[symbol]["correlations"]) ──
PRICE_ASSETS = {
    "EURUSD":  "EURUSD=X",
    "DXY":     "DX-Y.NYB",
    "US10Y":   "^TNX",
    "Gold":    "GC=F",
    "Oil_WTI": "CL=F",
    "SP500":   "^GSPC",
    "VIX":     "^VIX",
}
