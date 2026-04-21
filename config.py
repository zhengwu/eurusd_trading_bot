"""All tuneable parameters. No magic numbers in module code."""
from pathlib import Path

# ── Triage ────────────────────────────────────────────────────────────────────
TRIAGE_SCORE_THRESHOLD = 6       # Score >= this triggers full analysis
COOLDOWN_MINUTES = 45            # Minutes to suppress re-trigger after escalation
SCAN_INTERVAL_MINUTES = 30       # How often Tier 1 runs

# ── Pre-event agent ────────────────────────────────────────────────────────────
# Brief window: send scenario brief when event is between these many minutes away.
# Width must exceed SCAN_INTERVAL_MINUTES to guarantee detection despite scan jitter.
PRE_EVENT_BRIEF_MIN = 30         # Lower bound — don't send if event is < 30 min away
PRE_EVENT_BRIEF_MAX = 90         # Upper bound — don't send if event is > 90 min away
# Minimum surprise magnitude to trigger post-event full analysis.
# Filters out noise (trivial beats/misses well within rounding error).
PRE_EVENT_MIN_SURPRISE: dict[str, float] = {
    "NFP":       20_000,   # Nonfarm Payrolls — 20K jobs
    "CPI":       0.1,      # CPI / inflation — 0.1 percentage point
    "GDP":       0.1,      # GDP growth — 0.1 pp
    "RATE":      0.05,     # Rate decisions — 5 bps
    "DEFAULT":   0.05,     # Catch-all for everything else
}

# ── Models ────────────────────────────────────────────────────────────────────
TRIAGE_MODEL          = "claude-haiku-4-5-20251001"  # Fallback only — active triage uses Azure GPT-5.2 (see triage_prompt.py)
PRE_EVENT_BRIEF_MODEL = "claude-sonnet-4-6"          # Pre-event scenario brief — user-facing, quality matters
ANALYSIS_MODEL        = "claude-sonnet-4-6"

# Debate persona model — fallback only; Azure GPT-5.2 is used when AZURE_OPENAI_API_KEY is set.
# Haiku is fast and cheap; upgrade to Sonnet or Opus for higher-quality arguments.
# DEBATE_PERSONA_MODEL = "claude-opus-4-6"        # Advanced — richest persona arguments
# DEBATE_PERSONA_MODEL = "claude-sonnet-4-6"      # Balanced — good quality, moderate cost
DEBATE_PERSONA_MODEL   = "claude-haiku-4-5-20251001"  # Fallback only — active personas use Azure GPT-5.2

# Debate judge model — the synthesis step; quality matters more here than for personas.
# DEBATE_JUDGE_MODEL = "claude-opus-4-6"          # Advanced — sharpest CIO judgment
DEBATE_JUDGE_MODEL     = "claude-sonnet-4-6"      # Balanced — strong reasoning at moderate cost

# ── Regime Engine ──────────────────────────────────────────────────────────────
USE_REGIME_ANALYSIS = True   # Inject regime summary into LLM context window

# ── Debate outcome tracking ────────────────────────────────────────────────────
# Records pre/post-debate signals and fetches actual price at 8/16/24/48h via MT5.
# Zero token cost. Data saved to data/outcome_log.json.
# Set to False to disable entirely (no recording, no background checker thread).
DEBATE_OUTCOME_TRACKING = True

# ── Multi-Agent Debate / Ensemble ─────────────────────────────────────────────
USE_MULTI_AGENT_DEBATE  = True   # Run Bull/Bear/Devil's Advocate debate on Long/Short signals
DEBATE_MAX_UNCERTAINTY  = 75     # Signals with uncertainty_score > this are downgraded to Wait
                                 # Set conservatively at 75 pending calibration data — revisit after
                                 # ~20 tracked signals in outcome_log.json (target: 78-80 long-term)

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
# Covers full 24h-1h window: Tokyo (00-09), London (07-16), New York (12-21),
# Sydney (22-07 overlap). The 23:00-00:00 UTC gap (quiet late-Sydney) is acceptable.
# Note: is_market_hours() uses start <= hour < end and cannot wrap midnight, so
# the full 24h range is approximated as 00:00-23:00 (missing only 23:00-00:00 UTC).
MARKET_HOURS_START = 0           # 00:00 UTC — Tokyo / Singapore open
MARKET_HOURS_END = 23            # 23:00 UTC — late NY / early Sydney close

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
NEWS_MAX_AGE_HOURS = 8           # Drop articles older than this (reduces stale signal noise)

# ── Fast news watcher ─────────────────────────────────────────────────────────
FAST_WATCH_INTERVAL_MINUTES = 2  # How often fast watcher polls RSS feeds
FAST_COOLDOWN_MINUTES = 20       # Cooldown between fast-watcher escalations (shorter than main)

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
        "display":            "EUR/USD",
        "yf_ticker":          "EURUSD=X",
        "pip":                0.0001,
        "pip_value_per_lot":  10.0,
        "price_decimals":     5,
        "normal_spread_pips": 1.0,   # typical spread in quiet market hours
        "correlations": [
            ("DXY",    "DX-Y.NYB",   "",  "Inverse — DXY up = EUR/USD down"),
            ("Gold",   "GC=F",   "",  "Risk-on proxy — Gold up = mild EUR support"),
            ("US10Y",  "^TNX",   "%", "Yield — rising = USD strength pressure"),
            ("VIX",    "^VIX",   "",  "Fear index — VIX up = risk-off USD bid"),
            ("S&P500", "^GSPC",  "",  "Risk appetite — S&P up = mild risk-on"),
        ],
    },
    "GBPUSD": {
        "display":            "GBP/USD",
        "yf_ticker":          "GBPUSD=X",
        "pip":                0.0001,
        "pip_value_per_lot":  10.0,
        "price_decimals":     5,
        "normal_spread_pips": 1.5,
        "correlations": [
            ("DXY",    "DX-Y.NYB",    "",  "Inverse — DXY up = GBP/USD down"),
            ("FTSE",   "^FTSE",   "",  "UK equities — FTSE up = GBP support"),
            ("EURGBP", "EURGBP=X","",  "EUR/GBP cross — rising = GBP weakness"),
            ("US10Y",  "^TNX",    "%", "Yield — rising = USD strength"),
            ("VIX",    "^VIX",    "",  "Fear index — VIX up = risk-off"),
        ],
    },
    "USDJPY": {
        "display":            "USD/JPY",
        "yf_ticker":          "USDJPY=X",
        "pip":                0.01,
        "pip_value_per_lot":  6.7,    # ~100,000 * 0.01 / 150 — update if rate moves far
        "price_decimals":     3,
        "normal_spread_pips": 1.0,
        "correlations": [
            ("Nikkei", "^N225",  "",  "Japan equities — Nikkei up = JPY weakness"),
            ("US10Y",  "^TNX",   "%", "Yield — rising = USD/JPY up (carry trade)"),
            ("VIX",    "^VIX",   "",  "Fear index — VIX up = JPY safe-haven bid"),
            ("DXY",    "DX-Y.NYB",   "",  "USD index — DXY up = USD/JPY up"),
            ("Gold",   "GC=F",   "",  "Safe haven — Gold up = mixed JPY signal"),
        ],
    },
    "AUDUSD": {
        "display":            "AUD/USD",
        "yf_ticker":          "AUDUSD=X",
        "pip":                0.0001,
        "pip_value_per_lot":  10.0,
        "price_decimals":     5,
        "normal_spread_pips": 1.5,
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
JOB2_CHECK_INTERVAL_MINUTES  = 30    # How often Job 2 checks open positions

# Phase engine thresholds (R-multiples relative to original SL distance)
JOB2_LOSS_MILD_R             = -0.3  # 0 to this → LOSS_MILD (normal noise, monitor)
JOB2_LOSS_MODERATE_R         = -0.5  # mild to this → LOSS_MODERATE (alert, thesis review)
JOB2_LOSS_DEEP_R             = -0.7  # moderate to this → LOSS_DEEP (partial close eligible)
                                     # Below this → LOSS_CRITICAL (near SL, exit-biased)
JOB2_LOSS_DEEP_TRIM_PCT      = 50.0  # Suggested partial close % at LOSS_DEEP stage
JOB2_BREAKEVEN_R             = 1.0   # Move SL to breakeven when trade reaches this R
JOB2_BREAKEVEN_BUFFER_PIPS   = 3     # Set breakeven SL this many pips beyond entry (not at exact entry)
JOB2_PARTIAL_TP_R            = 1.5   # Suggest partial trim when trade reaches this R
JOB2_PARTIAL_TP_PCT          = 50.0  # Default trim size at partial TP (% of position)
JOB2_TRAIL_R                 = 2.0   # Start ATR-based trailing when trade reaches this R
JOB2_TRAIL_ATR_MULT          = 1.5   # Trail SL at this multiple of M15 ATR behind price
JOB2_OVERDUE_MULT            = 1.5   # Flag position overdue if open > N × expected horizon hours

# Operational safeguards
JOB2_MAX_SPREAD_MULT         = 2.0   # Skip SL/TP modifications if spread > N × normal_spread_pips
JOB2_NEWS_BLACKOUT_MIN       = 15    # Pause modifications if high-impact event is < N minutes away
JOB2_AUTO_BREAKEVEN          = True  # Auto-execute breakeven SL move without approval
JOB2_AUTO_TRAIL              = True  # Auto-execute trailing SL updates without approval

# Phase threshold watcher (lightweight tick-polling between the 30-min main loop)
JOB2_PHASE_WATCH_INTERVAL_SEC = 60   # How often the watcher polls MT5 ticks
JOB2_PHASE_WATCH_COOLDOWN_SEC = 300  # Min seconds between watcher-triggered analyses per position

# ── Job 3 — Trade Executor ────────────────────────────────────────────────────
JOB3_RISK_PCT                = 2.0   # % of account equity to risk per trade (base; scaled down by uncertainty)
JOB3_MIN_LOT                 = 0.01  # Minimum lot size
JOB3_MAX_LOT                 = 5.0   # Maximum lot size
JOB3_DEFAULT_SL_PIPS         = 30    # Fallback SL if key_levels missing
JOB3_DEFAULT_TP_PIPS         = 60    # Fallback TP if key_levels missing

# Risk:reward thresholds — two separate levels with distinct roles:
#
#   JOB3_MIN_RR      — hard execution block. Signals below this are rejected at execution
#                      time regardless of approval. Set to a genuine floor (not near-zero)
#                      to prevent clearly negative-edge trades.
#
#   JOB3_RR_WARNING  — soft visual warning. When computed R:R is below this, a ⚠ is shown
#                      in the Slack order preview and the analysis prompt instructs the model
#                      to flag it in risk_note. The trade can still be approved and executed.
#                      Raise to 2.0 once calibration data supports it.
JOB3_MIN_RR                  = 0.4    # Hard block: execution rejected below this R:R
JOB3_RR_WARNING              = 1.0    # Soft warn: ⚠ shown in Slack preview below this R:R
JOB3_SIGNAL_EXPIRY_MINUTES   = 60    # Signal auto-expires if not approved in time

# Uncertainty-based lot sizing — risk % multipliers by uncertainty tier
# uncertainty <= 30  → 1.00× (high conviction — full risk)
# uncertainty <= 55  → 0.75× (medium conviction)
# uncertainty <= 75  → 0.50× (low conviction — passed veto but sized down)
# uncertainty >  75  → blocked by DEBATE_MAX_UNCERTAINTY veto before reaching here
JOB3_UNCERTAINTY_TIERS: list[tuple[int, float]] = [
    (30, 1.00),
    (55, 0.75),
    (75, 0.50),
]

# Auto-approve and execute signals without human approval when uncertainty is at
# or below this threshold. Set to None to disable (all signals require approval).
JOB3_AUTO_APPROVE_MAX_UNCERTAINTY: int | None = 30

# Portfolio-level risk limits (applied before opening any new Long/Short)
JOB3_MAX_OPEN_TRADES         = 3     # Hard cap on concurrent positions across all pairs
JOB3_MAX_PORTFOLIO_RISK_PCT  = 5.0   # Max total risk (open positions + new trade) as % of equity
JOB3_MAX_CORRELATED_RISK_PCT = 3.0   # Max risk within same USD-direction bucket (usd_long/usd_short)

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
