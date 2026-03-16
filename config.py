"""All tuneable parameters. No magic numbers in module code."""
from pathlib import Path

# ── Triage ────────────────────────────────────────────────────────────────────
TRIAGE_SCORE_THRESHOLD = 6       # Score >= this triggers full analysis
COOLDOWN_MINUTES = 45            # Minutes to suppress re-trigger after escalation
SCAN_INTERVAL_MINUTES = 30       # How often Tier 1 runs

# ── Models ────────────────────────────────────────────────────────────────────
TRIAGE_MODEL = "claude-haiku-4-5-20251001"
ANALYSIS_MODEL = "claude-sonnet-4-6"

# ── Context window ────────────────────────────────────────────────────────────
CONTEXT_DAYS_SUMMARY = 7         # Days of summary.md to include
CONTEXT_DAYS_PRICES = 30         # Days of price trend to include
CONTEXT_MAX_TOKENS = 6000        # Hard cap — trim older prices first

# ── Market hours (UTC) — scanner only runs during these hours on weekdays ─────
MARKET_HOURS_START = 7           # 07:00 UTC
MARKET_HOURS_END = 21            # 21:00 UTC

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
MT5_SYMBOL = "EURUSD"
MT5_TIMEFRAME = "M15"
MT5_MAGIC_NUMBER = 20260314      # Identifies orders placed by this agent

# ── Job 2 — Position Monitor ──────────────────────────────────────────────────
JOB2_CHECK_INTERVAL_MINUTES = 30  # How often Job 2 checks open positions

# ── Job 3 — Trade Executor (planned) ──────────────────────────────────────────
JOB3_RISK_PCT = 1.0              # % of account equity to risk per trade
JOB3_MIN_LOT = 0.01              # Minimum lot size
JOB3_MAX_LOT = 1.0               # Maximum lot size
JOB3_DEFAULT_SL_PIPS = 30        # Fallback SL if key_levels missing
JOB3_DEFAULT_TP_PIPS = 60        # Fallback TP if key_levels missing
JOB3_SIGNAL_EXPIRY_MINUTES = 60  # Signal auto-expires if not approved in time

# ── Price assets to track ─────────────────────────────────────────────────────
PRICE_ASSETS = {
    "EURUSD":  "EURUSD=X",
    "DXY":     "DX=F",
    "US10Y":   "^TNX",
    "Gold":    "GC=F",
    "Oil_WTI": "CL=F",
    "SP500":   "^GSPC",
    "VIX":     "^VIX",
}
