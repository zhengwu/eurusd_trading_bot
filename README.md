# EUR/USD AI Trading Agent

An automated trading assistant for EUR/USD that monitors financial news, watches open positions, and executes trades on MetaTrader 5 — all controlled through Slack.

---

## Table of Contents

1. [What is this?](#what-is-this)
2. [How it works — the 4 jobs](#how-it-works--the-4-jobs)
3. [Requirements](#requirements)
4. [Installation and setup](#installation-and-setup)
   - [1. Download the code](#1-download-the-code)
   - [2. Create the Conda environment](#2-create-the-conda-environment)
   - [3. Create the .env file](#3-create-the-env-file)
   - [4. Set up a Slack app](#4-set-up-a-slack-app)
5. [Running the agent](#running-the-agent)
6. [Slack commands](#slack-commands)
7. [Approval workflow — a full example](#approval-workflow--a-full-example)
8. [File structure](#file-structure)
9. [Troubleshooting](#troubleshooting)

---

## What is this?

This is a Python-based AI agent that runs 24/5 (while the forex market is open) and does three things automatically:

1. **Reads the news.** Every 30 minutes it fetches EUR/USD-relevant headlines from multiple financial data providers, scores each one 1–10 for market relevance, and — when something important is detected — runs a deep analysis using Claude Sonnet to generate a trading signal (Long, Short, or Wait).

2. **Watches your open positions.** Every 30 minutes it connects to your MetaTrader 5 terminal, reads any open EUR/USD trades, and uses Claude Sonnet to recommend whether to Hold, Trim, or Exit each one based on current market conditions.

3. **Executes trades.** When you approve a signal (via Slack or the command line), the agent places or closes the order on MT5 directly, with position sizing calculated automatically from your account equity.

You stay in control. The agent never trades without your approval. Signals arrive in Slack, you type `approve XXXXXXXX`, and the trade executes. You can also reject signals, ask free-form questions, and have a full conversation with the agent about market conditions — all from Slack.

---

## How it works — the 4 jobs

The system is organised into four jobs. All four start automatically when you run the agent.

### Job 1 — Opportunity Scanner (runs every 30 minutes)

Job 1 is the core loop. It wakes up every 30 minutes during market hours, fetches the latest news from NewsAPI, AlphaVantage, and EODHD, and runs a fast triage model (Claude Haiku) over each headline to score it 1–10 for EUR/USD relevance.

If a headline scores 6 or above, Job 1 triggers a full deep analysis using Claude Sonnet. It feeds the model a rich context package — recent EURUSD prices, correlated assets (DXY, Gold, US10Y, S&P 500, VIX), today's news log, and the past week of daily summaries — and asks for a structured signal:

```
Signal     : Long
Confidence : High
Horizon    : Intraday
Rationale  : ECB minutes showed a more dovish tone than expected...
Key Levels : Support 1.0820 | Resistance 1.0910
Invalidation: Close below 1.0800
Risk Note  : NFP data tomorrow at 13:30 UTC — reduce size
```

This signal is posted to Slack and printed to the console. If the action is Long or Short, a pending signal is saved to the store and the Slack message includes the approval command.

After a trigger, Job 1 enters a 45-minute cooldown to avoid sending repeated alerts on the same news event.

Job 1 also runs an end-of-day collector at 22:00 UTC every day in a background thread. This fetches closing prices and news, writes daily summary files to the `data/` folder, and cleans up old signals from the store.

### Job 2 — Position Monitor (runs every 30 minutes)

Job 2 runs as a background thread alongside Job 1. It connects to your MT5 terminal and reads all open EUR/USD positions. For each open position it calls Claude Sonnet with the position details (entry price, current P&L, SL/TP levels), account health (equity, margin level, drawdown), and the current market snapshot.

Claude returns one of three recommendations:

- **Hold** — the position is on track, no action needed.
- **Trim** — risk/reward has deteriorated but the direction is still valid; partially close the position.
- **Exit** — the original trade thesis is broken; close the position in full.

Hold signals are informational only. Trim and Exit recommendations are saved as pending signals and posted to Slack with an approval command. You decide whether to act.

### Job 3 — Trade Executor

Job 3 is the execution engine. It is not a loop — it runs when you approve a signal (either from Slack or the command line). It reads the approved signal from the store and routes it to the correct MT5 action:

| Signal source | Signal type | MT5 action |
|---------------|-------------|------------|
| Job 1 | Long | Open BUY market order |
| Job 1 | Short | Open SELL market order |
| Job 2 / Job 4 | Exit | Close full position |
| Job 2 / Job 4 | Trim | Close partial position (default 50%) |
| Job 4 | SetSL / SetTP / SetSLTP | Modify stop loss / take profit |

For new positions (Long/Short), Job 3 calculates lot size automatically: it risks exactly 1% of your current account equity on each trade, using the stop loss distance in pips to determine the correct lot size. The result is clamped between 0.01 and 1.0 lots.

### Job 4 — Chat Assistant

Job 4 is a conversational interface to the entire system. You can DM the Slack bot or @mention it in a channel and ask anything in plain English:

- "What's the EUR/USD situation right now?"
- "Show me my open positions"
- "The news is looking bearish — can you propose a short with SL at 1.0850?"

Job 4 uses Claude Sonnet with tool calling. It can fetch live prices, read open positions, pull recent news, check pending signals, and — when you ask it to take a trading action — call an internal `propose_order` tool to create a pending signal that goes through the normal approval flow.

Conversations are threaded. Each Slack thread keeps its own 2-hour history so the agent remembers context within a conversation.

---

## Requirements

**Operating system:** Windows only. The MetaTrader 5 Python package (`MetaTrader5`) only runs on Windows.

**MetaTrader 5 terminal:** The MT5 desktop application must be running and logged into your broker account before you start the agent. The agent connects to the already-running terminal — it does not launch MT5 itself.

**Python environment:** Anaconda or Miniconda. The instructions below use a Conda environment named `mt5_env`.

**External accounts required:**

| Service | What it is used for | Free tier available? |
|---------|---------------------|----------------------|
| Anthropic | Claude Haiku (triage) + Claude Sonnet (analysis/chat) | No — pay per token |
| NewsAPI | Financial news headlines | Yes (developer plan, 100 req/day) |
| AlphaVantage | Financial news + sentiment | Yes (25 req/day free) |
| EODHD | Financial news headlines | Yes (limited free tier) |
| Slack | Notifications + two-way command interface | Yes |

**Slack app:** You need to create a Slack app with Socket Mode enabled. Step-by-step instructions are below.

---

## Installation and setup

### 1. Download the code

If you have Git installed, open Anaconda Prompt and run:

```bash
git clone <repository-url> C:\Users\<yourname>\Documents\eurusd_agent
```

If you do not have Git, download the ZIP from the repository page and extract it to `C:\Users\<yourname>\Documents\eurusd_agent`.

### 2. Create the Conda environment

Open Anaconda Prompt (not regular Command Prompt — you need the Conda tools) and run these commands one at a time:

```bash
conda create -n mt5_env python=3.11 -y
conda activate mt5_env
pip install MetaTrader5
pip install slack-bolt
pip install -r C:\Users\<yourname>\Documents\eurusd_agent\requirements.txt
```

`MetaTrader5` and `slack-bolt` are installed separately. `MetaTrader5` is Windows-only and not in the shared requirements file. `slack-bolt` is only needed if you want the two-way Slack command interface (you still get one-way Slack webhook alerts without it).

Verify the installation worked:

```bash
cd C:\Users\<yourname>\Documents\eurusd_agent
python verify_imports.py
```

This script checks that all required packages import correctly and will tell you if anything is missing.

### 3. Create the .env file

In the `eurusd_agent` folder, create a file named exactly `.env` (the filename starts with a dot and has no extension). In Notepad, save it as type "All Files" and name it `.env`.

Paste the template below and fill in your own values:

```
# Anthropic — https://console.anthropic.com/
ANTHROPIC_API_KEY=sk-ant-...

# NewsAPI — https://newsapi.org/register
NEWS_API_KEY=...

# AlphaVantage — https://www.alphavantage.co/support/#api-key
ALPHAVANTAGE_API_KEY=...

# EODHD — https://eodhd.com/register
EODHD_API_KEY=...

# Slack — incoming webhook (one-way signal alerts posted to a channel)
# Get this from your Slack app's "Incoming Webhooks" page
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

# Slack — Socket Mode bot (two-way commands: approve, reject, chat, etc.)
# SLACK_BOT_TOKEN: "Bot User OAuth Token" on the OAuth & Permissions page (starts with xoxb-)
# SLACK_APP_TOKEN: "App-Level Token" on the Basic Information page (starts with xapp-)
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# Email alerts (optional)
# To disable email, leave SMTP_USER and SMTP_PASSWORD blank or remove these lines.
# For Gmail, use an App Password, not your regular password:
# https://support.google.com/accounts/answer/185833
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@gmail.com
SMTP_PASSWORD=your-app-password
```

**Where to get each key:**

- **ANTHROPIC_API_KEY** — Sign up at https://console.anthropic.com, go to API Keys, and create a new key. This key costs money every time a model is called; keep it private.
- **NEWS_API_KEY** — Register at https://newsapi.org/register. The free developer plan allows 100 requests per day.
- **ALPHAVANTAGE_API_KEY** — Get a free key at https://www.alphavantage.co/support/#api-key. The free tier allows 25 requests per day.
- **EODHD_API_KEY** — Register at https://eodhd.com. A limited free tier is available.
- **SLACK_WEBHOOK_URL** — Created during Slack app setup (Step 4 below).
- **SLACK_BOT_TOKEN** — Created during Slack app setup (Step 4 below).
- **SLACK_APP_TOKEN** — Created during Slack app setup (Step 4 below).

### 4. Set up a Slack app

This is the most involved setup step. The Slack integration has two parts: a one-way incoming webhook (for posting signal alerts) and a two-way Socket Mode bot (for receiving your commands). Both are configured inside the same Slack app.

#### Step 4a — Create the app

1. Go to https://api.slack.com/apps and click **Create New App**.
2. Choose **From scratch**.
3. Give the app a name such as `EURUSD Agent`.
4. Select your workspace and click **Create App**.

#### Step 4b — Add bot token scopes

1. In the left sidebar, click **OAuth & Permissions**.
2. Scroll down to **Bot Token Scopes** and click **Add an OAuth Scope**.
3. Add all four of the following scopes:
   - `chat:write` — allows the bot to post messages
   - `im:history` — allows the bot to read direct messages sent to it
   - `channels:history` — allows the bot to read messages in channels it joins
   - `app_mentions:read` — allows the bot to receive @mention events

#### Step 4c — Enable incoming webhooks (for signal alerts)

1. In the left sidebar, click **Incoming Webhooks**.
2. Toggle **Activate Incoming Webhooks** to On.
3. Click **Add New Webhook to Workspace**.
4. Choose the channel where you want signal alerts to appear (for example, `#eurusd-alerts`). You can create this channel in Slack first if it does not exist.
5. Click **Allow**.
6. Copy the webhook URL that appears (it starts with `https://hooks.slack.com/services/`). Paste it into `.env` as `SLACK_WEBHOOK_URL`.

#### Step 4d — Enable Socket Mode (for two-way commands)

1. In the left sidebar, click **Socket Mode**.
2. Toggle **Enable Socket Mode** to On.
3. When prompted to create an App-Level Token, give it a name such as `socket-token` and tick the `connections:write` scope.
4. Click **Generate**.
5. Copy the token shown (it starts with `xapp-`). Paste it into `.env` as `SLACK_APP_TOKEN`.

#### Step 4e — Subscribe to bot events

1. In the left sidebar, click **Event Subscriptions**.
2. Toggle **Enable Events** to On.
3. Under **Subscribe to bot events**, click **Add Bot User Event** and add all three:
   - `message.im` — receives direct messages
   - `message.channels` — receives messages in channels the bot has joined
   - `app_mention` — receives @mentions
4. Click **Save Changes** at the bottom of the page.

#### Step 4f — Install the app and get the bot token

1. In the left sidebar, click **OAuth & Permissions**.
2. Click **Install to Workspace** (or **Reinstall to Workspace** if the button says reinstall).
3. Review the permissions and click **Allow**.
4. Copy the **Bot User OAuth Token** (it starts with `xoxb-`). Paste it into `.env` as `SLACK_BOT_TOKEN`.

#### Step 4g — Invite the bot to your channel

In Slack, open the channel you chose for alerts and type:

```
/invite @EURUSD Agent
```

The bot must be a member of the channel to post messages to it. This step is easy to forget.

---

## Running the agent

Before starting, make sure MetaTrader 5 is open and logged into your broker account.

Open Anaconda Prompt and run:

```bash
conda activate mt5_env
cd C:\Users\<yourname>\Documents\eurusd_agent
python -m agents.job1_opportunity
```

You will see startup output similar to this:

```
============================================================
EUR/USD Opportunity Scanner — Job 1
  Triage model  : claude-haiku-4-5-20251001
  Analysis model: claude-sonnet-4-6
  Scan interval : 30 min
  Market hours  : 7:00 – 21:00 UTC
  Daily collect : 22:00 UTC
  Job 2 interval: 30 min
  Notifications : ['print', 'slack']
============================================================
Daily collection scheduler running in background
Job 2 position monitor starting in background
Slack bot starting in background
```

All four jobs start from this single command. Job 1's scanner loop runs in the foreground. Jobs 2, the daily collector, and the Slack bot run as background threads. The process blocks until you press `Ctrl+C`.

**To manage signals from the command line instead of Slack:**

```bash
# List pending, approved, and recently executed signals
python -m agents.job3_executor --list

# Approve a signal and execute it immediately on MT5
python -m agents.job3_executor --approve A1B2C3D4

# Approve a Trim signal and close only 25% of the position (default is 50%)
python -m agents.job3_executor --approve A1B2C3D4 --pct 25

# Reject a signal (no trade is placed)
python -m agents.job3_executor --reject A1B2C3D4
```

Signal IDs are 8-character alphanumeric codes such as `A1B2C3D4`. They appear in Slack alert messages and in the `--list` output. IDs are case-insensitive.

---

## Slack commands

Send any of these as a direct message to the bot, or @mention the bot in your alerts channel (for example `@EURUSD Agent scan`).

| Command | What it does |
|---------|--------------|
| `scan` or `update` | Force an immediate news scan, bypassing the 30-minute timer and market hours check |
| `positions` | Run a Job 2 position check right now |
| `status` | Show system health: cooldown timer remaining, MT5 connection state, number of pending signals |
| `collect` | Trigger the end-of-day data collection immediately |
| `pending` | List all signals currently waiting for your approval, with their IDs |
| `approve <ID>` | Approve a pending signal and execute it on MT5 |
| `approve <ID> <pct>` | Approve a Trim signal and close `<pct>` percent of the position (e.g. `approve A1B2C3D4 25`) |
| `reject <ID>` | Reject a pending signal — no trade is placed |
| `help` | Show the command list |
| `hi` / `hello` / `who are you` | The bot introduces itself and explains what it does |
| _Any other message_ | Starts or continues a free-form conversation with the Job 4 chat assistant |

**Examples:**

```
scan
positions
status
pending
approve A1B2C3D4
approve A1B2C3D4 25
reject A1B2C3D4
What does the EUR/USD chart look like right now?
Is now a good time to add to my long?
Propose a short with SL at 1.0920 and TP at 1.0750
```

The bot also understands natural language variants of commands, not just exact keywords. "Fetch the latest news", "check my portfolio", and "what's the system status?" are all understood correctly through an NLP classifier. If the NLP classifier cannot determine the intent, the message is routed to the Job 4 chat assistant.

---

## Approval workflow — a full example

Here is what a complete signal lifecycle looks like, from detection to execution.

---

**09:15 UTC — Job 1 detects a high-impact news event**

The scanner picks up a headline: "ECB officials signal no further rate hikes in 2026". The triage model (Claude Haiku) scores it 8/10. This is above the threshold of 6, so full analysis is triggered.

Claude Sonnet analyses the headline in context with current prices, DXY movements, and recent market summaries. It produces a Long signal and the agent posts it to Slack.

---

**09:15 UTC — Slack alert arrives in #eurusd-alerts**

```
EUR/USD Long  [High] — Intraday
2026-03-16 09:15 UTC
Trigger [8]: ECB officials signal no further rate hikes in 2026 (central_bank)

Rationale: ECB pivot narrative supports EUR bulls. DXY weakening on USD
yield compression. Structure aligns with bullish breakout above
the 1.0870 resistance turned support.

Key Levels: Support 1.0870 | Resistance 1.0960
Invalidation: Hourly close below 1.0845
Risk Note: Fed speaker at 14:00 UTC — may cause short-term volatility

To execute: python -m agents.job3_executor --approve 3F8A1C09
To reject:  python -m agents.job3_executor --reject 3F8A1C09
```

(If the Slack bot is running, you can also just type `approve 3F8A1C09` directly in Slack.)

---

**09:17 UTC — You review and approve**

You read the signal, agree with the reasoning, and type in Slack:

```
approve 3F8A1C09
```

---

**09:17 UTC — Job 3 executes the trade**

The bot responds immediately in Slack:

```
Signal `3F8A1C09` executed!
Ticket:      12345678
Fill price:  1.08923
Volume:      0.08 lots
```

Behind the scenes, Job 3 did the following:
1. Retrieved the signal from the store and confirmed it was still pending (not expired).
2. Marked the signal as approved.
3. Fetched the current ask price from MT5 (1.08923).
4. Derived SL = 1.0870 and TP = 1.0960 from the signal's key levels.
5. Calculated lot size: with $10,000 equity, risking 1% ($100), and a 53-pip stop loss ($10/pip per standard lot), the formula gives $100 / (53 × $10) = 0.189 lots, clamped to 0.08 lots after rounding to two decimal places and applying broker limits.
6. Sent a BUY market order to MT5 tagged with magic number `20260314` so it can be identified as coming from this agent.

---

**11:00 UTC — Job 2 checks the open position**

Thirty minutes after the trade opened, Job 2 wakes up, reads the position, and calls Claude Sonnet. EUR/USD is now at 1.0940 and the trade is up $136. Claude recommends Hold:

```
Action    : Hold
Confidence: High
Rationale : Position tracking toward TP at 1.0960. Structure intact above
            1.0870 support. No reason to exit early.
```

Because the recommendation is Hold, no pending signal is created and no approval is needed. The update is posted to Slack as an informational message only.

---

**What if the recommendation were Exit instead?**

If conditions deteriorated and Claude recommended Exit, the Slack message would look like this:

```
Position Monitor — #12345678 BUY 0.08L @ 1.08923 | P&L: -$45
Action: Exit  [Medium]

Rationale: Price broke below the 1.0845 invalidation level on the last
hourly close. Original thesis is no longer valid.

To execute: approve 9D2E4B11
To reject:  reject 9D2E4B11
```

You type `approve 9D2E4B11` and Job 3 closes the full position at the current bid price.

---

**What if you only want to close part of the position?**

For a Trim recommendation, you can control how much is closed by including a percentage:

```
approve 9D2E4B11 25
```

This closes 25% of the 0.08-lot position (0.02 lots), leaving 0.06 lots open. If you omit the percentage, the default is 50%.

---

**Signals expire automatically**

If you do not approve or reject a signal within 60 minutes of its creation, it expires automatically. Expired signals cannot be executed. The end-of-day cleanup (runs daily at 22:00 UTC) removes signals older than 7 days from the store. You can check the current status of all signals with:

```
pending
```

---

**Using the chat assistant to propose a trade**

Instead of waiting for Job 1 to generate a signal, you can ask Job 4 to propose one directly. Start a conversation in Slack by DMing the bot or @mentioning it:

```
@EURUSD Agent The dollar is looking weak after the CPI miss. Can you check
the current EURUSD chart and propose a long if the setup looks clean?
```

Job 4 will call its tools to fetch live data, analyse the situation, and if appropriate call `propose_order` internally. It will reply with its reasoning and post a signal ID:

```
Order proposed — Signal ID: `B7C3D912`
To execute: approve B7C3D912
To cancel:  reject B7C3D912
```

You approve or reject it the same way as any other signal.

---

## File structure

```
eurusd_agent/
├── config.py                  # All tunable parameters — edit this to change behaviour
├── requirements.txt           # Python package dependencies
├── verify_imports.py          # Quick import check after install
│
├── agents/
│   ├── job1_opportunity.py    # Entry point — starts all four jobs
│   ├── job2_position.py       # Position monitor loop
│   ├── job3_executor.py       # Trade execution engine + CLI
│   ├── job4_chat.py           # Conversational assistant (Claude Sonnet + tools)
│   └── slack_bot.py           # Slack Socket Mode bot + command dispatcher
│
├── triage/
│   ├── scanner.py             # 30-minute news scan loop
│   ├── triage_prompt.py       # Claude Haiku prompt for scoring headlines
│   ├── intraday_logger.py     # Logs today's scored headlines to disk
│   └── cooldown.py            # 45-minute cooldown lock management
│
├── analysis/
│   ├── context_builder.py     # Assembles the context package for Sonnet
│   ├── full_analysis_prompt.py # Sonnet prompt for full trade analysis
│   └── signal_formatter.py    # Validates and formats signal output
│
├── pipeline/
│   ├── signal_store.py        # Pending signal queue (data/pending_signals.json)
│   ├── daily_collector.py     # End-of-day price + summary collection
│   ├── news_fetcher.py        # NewsAPI, AlphaVantage, EODHD fetchers
│   ├── price_fetcher.py       # yfinance price data (EURUSD, DXY, Gold, etc.)
│   ├── event_fetcher.py       # Economic calendar events
│   └── dedup_cache.py         # SHA-256 deduplication to avoid processing the same headline twice
│
├── mt5/
│   ├── connector.py           # MT5 connect / disconnect / connection check
│   ├── position_reader.py     # Read open positions, account info, live ticks
│   ├── order_manager.py       # Open, close, and modify positions
│   └── risk_manager.py        # Lot sizing (1% risk) and SL/TP calculation
│
├── notifications/
│   └── notifier.py            # Dispatches alerts to Slack webhook, email, and console
│
├── utils/
│   ├── logger.py              # Rotating file + console logging
│   └── date_utils.py          # Market hours checks, UTC/EST timezone helpers
│
└── data/                      # Created automatically on first run (not committed to git)
    ├── pending_signals.json   # Live signal queue — human-readable, safe to inspect
    ├── .cooldown_lock         # Written when cooldown is active, deleted when it expires
    ├── prices/                # Daily OHLCV price files (one JSON per day)
    ├── news/                  # Daily news logs (one JSONL per day)
    └── summaries/             # Daily market summary markdown files
```

---

## Troubleshooting

**"MT5 initialize() failed" on startup**

The agent cannot connect to a running MetaTrader 5 terminal. Check that:
- MetaTrader 5 is open and fully loaded, not just minimised to the system tray.
- You are logged into your broker account inside MT5 (the terminal shows live prices).
- You are running the agent on the same Windows machine as MT5. Remote connections are not supported.

**"SLACK_APP_TOKEN not set — Slack bot will not start"**

The `.env` file is missing one or both Slack bot tokens. Check that:
- The `.env` file exists in the root of the `eurusd_agent` folder.
- Both `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are present and have non-empty values.
- You ran `conda activate mt5_env` before starting the agent (the `.env` file is loaded by the Python process, not by the shell).

**Signal alerts appear in Slack but the approve/reject commands do nothing**

This happens when the Slack bot (Socket Mode) is not running but the webhook is working. The webhook posts messages but cannot receive replies. Confirm that `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are both set in `.env` and that the agent startup log shows "Slack bot connected" rather than a warning about missing tokens.

**The agent starts but no Slack messages appear at all**

Check `SLACK_WEBHOOK_URL` in `.env`. Make sure you copied the full URL including `https://`. Also confirm the bot has been invited to the channel with `/invite @EURUSD Agent`.

**"Symbol EURUSD not found in MT5"**

The EURUSD symbol is not visible in your MT5 Market Watch. Open MT5, go to View > Market Watch, right-click anywhere in the list, choose "Show All" or search for EURUSD, and add it. The agent requires the symbol to be visible before it can connect.

**"ModuleNotFoundError: No module named 'MetaTrader5'"**

The package was installed in the wrong Python environment. Run:

```bash
conda activate mt5_env
pip install MetaTrader5
```

**"ModuleNotFoundError: No module named 'slack_bolt'"**

Same issue — the package is not in the active environment:

```bash
conda activate mt5_env
pip install slack-bolt
```

**Job 2 is not checking positions even though MT5 is connected**

Job 2 only runs during market hours (07:00–21:00 UTC on weekdays). It is paused from Friday 17:00 EST to Sunday 17:00 EST when the forex market is closed. To force an immediate check at any time, use the `positions` Slack command or run:

```bash
python -c "from agents.job2_position import run_position_check; run_position_check()"
```

**Signals are expiring before you can approve them**

The default expiry is 60 minutes (`JOB3_SIGNAL_EXPIRY_MINUTES` in `config.py`). If you are not always at your desk, increase this value. Be aware that a very old signal may propose a trade at prices that no longer reflect current conditions.

**You want to change the scan frequency, risk percentage, or market hours**

All tunable parameters are in `config.py`. The key ones:

| Parameter | Default | What it controls |
|-----------|---------|-----------------|
| `SCAN_INTERVAL_MINUTES` | 30 | How often Job 1 scans news |
| `TRIAGE_SCORE_THRESHOLD` | 6 | Minimum score to trigger a full analysis |
| `COOLDOWN_MINUTES` | 45 | Silence period after a trigger fires |
| `JOB2_CHECK_INTERVAL_MINUTES` | 30 | How often Job 2 checks open positions |
| `JOB3_RISK_PCT` | 1.0 | Percentage of account equity risked per trade |
| `JOB3_SIGNAL_EXPIRY_MINUTES` | 60 | How long a signal stays valid before auto-expiring |
| `MARKET_HOURS_START` / `MARKET_HOURS_END` | 7 / 21 | Active scanning window (UTC hour) |

Edit `config.py` and restart the agent for changes to take effect.
