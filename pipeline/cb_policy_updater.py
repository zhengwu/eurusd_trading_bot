"""Central bank policy updater.

Triggered by the triage scanner whenever a CB_speech or cb_decision headline
is detected. Fetches the latest official statement from the relevant central
bank's website, extracts stance/guidance/expected using Claude Haiku, and
writes the result to data/rate_cycles.json.

config.RATE_CYCLES    = baseline values (edited manually after meetings)
data/rate_cycles.json = runtime overrides (auto-updated by this module)

Both are merged at analysis time by get_rate_cycles() below, with the JSON
file taking precedence over the config baseline.

Official statement sources (all free, no API key):
  Fed : RSS  https://www.federalreserve.gov/feeds/press_monetary.xml
  ECB : RSS  https://www.ecb.europa.eu/rss/press.html
            (filtered for "Monetary policy decisions" items)
  BOE : HTML https://www.bankofengland.co.uk/monetary-policy/the-interest-rate-bank-rate
            (follows first monetary-policy-summary-and-minutes link)
  BOJ : PDF  https://www.boj.or.jp/en/mopo/mpmdeci/mpr_{YYYY}/k{YYMMDD}a.pdf
            (pdfminer.six used for text extraction — pip install pdfminer.six)
"""
from __future__ import annotations

import io
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import date
from html.parser import HTMLParser

import anthropic
import requests
from dotenv import load_dotenv

import config
from utils.logger import get_logger
from utils.retry import call_with_retry

load_dotenv()
logger = get_logger(__name__)

_OVERRIDE_FILE = config.DATA_DIR / "rate_cycles.json"
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT = 15


# ── HTML -> plain text ────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "li", "br", "tr", "td"}
    _SKIP  = {"script", "style", "nav", "footer", "header"}

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skipping = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping = True
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skipping = False

    def handle_data(self, data):
        if not self._skipping:
            self._parts.append(data)

    def text(self, max_chars: int = 6000) -> str:
        t = "".join(self._parts)
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()[:max_chars]


def _html_to_text(html: str, max_chars: int = 6000) -> str:
    p = _TextExtractor()
    p.feed(html)
    return p.text(max_chars)


def _pdf_to_text(content: bytes, max_chars: int = 6000) -> str:
    """Extract text from PDF bytes using pdfminer.six."""
    from pdfminer.high_level import extract_text
    text = extract_text(io.BytesIO(content))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_chars]


# ── Per-bank statement fetchers ───────────────────────────────────────────────

def _fetch_fed() -> str | None:
    """
    Fetch latest Fed monetary policy press release via RSS feed.
    Source: https://www.federalreserve.gov/feeds/press_monetary.xml
    """
    try:
        r = requests.get(
            "https://www.federalreserve.gov/feeds/press_monetary.xml",
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)

        # Try Atom namespace first, then plain RSS 2.0
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//a:entry", ns)
        if entries:
            link_el = entries[0].find("a:link", ns)
            url = link_el.get("href") if link_el is not None else None
        else:
            item = root.find(".//item")
            url = item.findtext("link") if item is not None else None

        if not url:
            logger.warning("Fed RSS: no statement URL found")
            return None

        logger.info(f"Fed statement: {url}")
        r2 = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        r2.raise_for_status()
        return _html_to_text(r2.text)
    except Exception as e:
        logger.warning(f"Fed fetch failed: {e}")
        return None


def _fetch_ecb() -> str | None:
    """
    Fetch latest ECB 'Monetary policy decisions' press release via RSS.
    Source: https://www.ecb.europa.eu/rss/press.html
    Filters for items with 'Monetary policy decisions' in the title.
    """
    try:
        r = requests.get(
            "https://www.ecb.europa.eu/rss/press.html",
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)

        url = None
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            if "monetary policy decisions" in title.lower():
                url = item.findtext("link", "").strip()
                break

        if not url:
            logger.warning("ECB RSS: no 'Monetary policy decisions' item found")
            return None

        logger.info(f"ECB statement: {url}")
        r2 = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        r2.raise_for_status()
        return _html_to_text(r2.text)
    except Exception as e:
        logger.warning(f"ECB fetch failed: {e}")
        return None


def _fetch_boe() -> str | None:
    """
    Fetch latest BOE MPC monetary policy summary.
    Source: https://www.bankofengland.co.uk/monetary-policy/the-interest-rate-bank-rate
    Follows the first monetary-policy-summary-and-minutes/{year}/{month-year} link.
    """
    try:
        r = requests.get(
            "https://www.bankofengland.co.uk/monetary-policy/the-interest-rate-bank-rate",
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        r.raise_for_status()

        m = re.search(
            r'href="(/monetary-policy-summary-and-minutes/\d{4}/[^"]+)"',
            r.text,
        )
        if not m:
            logger.warning("BOE: no MPC summary link found")
            return None

        url = "https://www.bankofengland.co.uk" + m.group(1)
        logger.info(f"BOE statement: {url}")
        r2 = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        r2.raise_for_status()
        return _html_to_text(r2.text)
    except Exception as e:
        logger.warning(f"BOE fetch failed: {e}")
        return None


def _fetch_boj() -> str | None:
    """
    Fetch latest BOJ Statement on Monetary Policy (PDF).
    Source: https://www.boj.or.jp/en/mopo/mpmdeci/mpr_{YYYY}/index.htm
    Finds the most recent 'k' prefix PDF (key policy statement) and extracts text.
    Requires pdfminer.six (pip install pdfminer.six).
    """
    try:
        year = date.today().year
        index_url = f"https://www.boj.or.jp/en/mopo/mpmdeci/mpr_{year}/index.htm"
        r = requests.get(index_url, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()

        # 'k' prefix PDFs are the key policy statements (e.g. k260319a.pdf)
        pdfs = re.findall(
            rf'href="(/en/mopo/mpmdeci/mpr_{year}/k\d+[^"]*\.pdf)"',
            r.text,
        )
        if not pdfs:
            logger.warning(f"BOJ: no statement PDF found in {index_url}")
            return None

        url = "https://www.boj.or.jp" + pdfs[0]
        logger.info(f"BOJ statement: {url}")
        r2 = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        r2.raise_for_status()
        return _pdf_to_text(r2.content)
    except Exception as e:
        logger.warning(f"BOJ fetch failed: {e}")
        return None


_FETCHERS = {
    "Fed": _fetch_fed,
    "ECB": _fetch_ecb,
    "BOE": _fetch_boe,
    "BOJ": _fetch_boj,
}


# ── Haiku extraction ──────────────────────────────────────────────────────────

_EXTRACT_PROMPT = """\
You are a central bank policy analyst. Read the official statement below and extract the current monetary policy stance.

Return ONLY valid JSON, no other text:
{{
  "stance": "Hiking" or "Pausing" or "Cutting",
  "guidance": "One sentence: the key forward guidance from this statement (what they signaled about future moves)",
  "expected": "One sentence: what this statement implies the market should expect over the next 6-12 months"
}}

Official statement:
{text}"""


def _extract_policy(statement: str) -> dict | None:
    """Use Claude Haiku to extract stance/guidance/expected from a statement."""
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        msg = call_with_retry(
            client.messages.create,
            model=config.TRIAGE_MODEL,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": _EXTRACT_PROMPT.format(text=statement[:5000]),
            }],
        )
        raw   = msg.content[0].text.strip()
        clean = re.sub(r"^```[a-z]*\n?", "", raw)
        clean = re.sub(r"\n?```$", "", clean).strip()
        result = json.loads(clean)
        if not isinstance(result, dict):
            raise ValueError("not a dict")
        if result.get("stance") not in ("Hiking", "Pausing", "Cutting"):
            raise ValueError(f"invalid stance: {result.get('stance')!r}")
        return result
    except Exception as e:
        logger.error(f"Policy extraction failed: {e}")
        return None


# ── Override file helpers ─────────────────────────────────────────────────────

def _read_overrides() -> dict:
    try:
        if _OVERRIDE_FILE.exists():
            return json.loads(_OVERRIDE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_overrides(data: dict) -> None:
    _OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OVERRIDE_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Per-day dedup (in-memory) ─────────────────────────────────────────────────

_fetched_today: set[str] = set()


def _already_fetched(bank: str) -> bool:
    return f"{bank}_{date.today().isoformat()}" in _fetched_today


def _mark_fetched(bank: str) -> None:
    _fetched_today.add(f"{bank}_{date.today().isoformat()}")


# ── Public API ────────────────────────────────────────────────────────────────

def update_bank_policy(bank: str) -> bool:
    """
    Fetch the latest official statement for `bank`, extract the policy stance
    with Claude Haiku, and persist the result to data/rate_cycles.json.

    Returns True if the override file was updated, False otherwise.
    Skips silently if the same bank was already fetched today.
    """
    if bank not in _FETCHERS:
        logger.warning(f"cb_policy_updater: unknown bank '{bank}' — skipping")
        return False

    if _already_fetched(bank):
        logger.debug(f"cb_policy_updater: {bank} already processed today — skipping")
        return False

    _mark_fetched(bank)
    logger.info(f"cb_policy_updater: [{bank}] fetching latest official statement...")

    statement = _FETCHERS[bank]()
    if not statement:
        logger.warning(f"cb_policy_updater: [{bank}] statement fetch failed")
        return False

    logger.info(f"cb_policy_updater: [{bank}] extracting policy ({len(statement)} chars)...")
    extracted = _extract_policy(statement)
    if not extracted:
        logger.warning(f"cb_policy_updater: [{bank}] extraction failed")
        return False

    today_str  = date.today().isoformat()
    overrides  = _read_overrides()
    old_stance = (
        overrides.get(bank, {}).get("stance")
        or config.RATE_CYCLES.get(bank, {}).get("stance", "Unknown")
    )
    new_entry = {
        "stance":   extracted["stance"],
        "guidance": extracted["guidance"],
        "expected": extracted["expected"],
        "updated":  today_str,
        "_source":  "auto",
    }

    overrides[bank] = new_entry
    _write_overrides(overrides)

    # Build notification message
    change_tag = " [STANCE CHANGED]" if extracted["stance"] != old_stance else ""
    msg = (
        f":bank: CB Policy Update: {bank}{change_tag}\n"
        f"  Stance   : {old_stance} -> {extracted['stance']}\n"
        f"  Guidance : {extracted['guidance']}\n"
        f"  Expected : {extracted['expected']}\n"
        f"  Updated  : {today_str} (auto)"
    )
    logger.info(msg)

    try:
        from notifications.notifier import notify_text
        notify_text(msg)
    except Exception as e:
        logger.warning(f"cb_policy_updater: notify failed: {e}")

    return True


def get_rate_cycles() -> dict:
    """
    Return the merged rate cycle dict:
      config.RATE_CYCLES (baseline) overridden by data/rate_cycles.json (auto-updated).

    Use this everywhere instead of reading config.RATE_CYCLES directly.
    """
    base      = dict(getattr(config, "RATE_CYCLES", {}))
    overrides = _read_overrides()
    for bank, entry in overrides.items():
        base[bank] = {**base.get(bank, {}), **entry}
    return base
