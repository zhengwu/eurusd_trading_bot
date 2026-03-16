"""Date helpers: market hours, UTC/EST conversion.

The user is in EST. MT5 server times and data APIs may use UTC — always
convert display-facing output to EST before presenting to the user.
"""
from datetime import date, datetime, timedelta, timezone
import zoneinfo

EST = zoneinfo.ZoneInfo("America/New_York")
UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_est() -> datetime:
    return datetime.now(EST)


def to_est(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(EST)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def today_str_utc() -> str:
    """Return today's date as YYYY-MM-DD (UTC)."""
    return datetime.now(UTC).date().isoformat()


def today_str_est() -> str:
    """Return today's date as YYYY-MM-DD (EST — user's local date)."""
    return datetime.now(EST).date().isoformat()


def is_market_hours(start_utc: int, end_utc: int) -> bool:
    """Return True if current UTC hour is within [start_utc, end_utc)."""
    hour = datetime.now(UTC).hour
    return start_utc <= hour < end_utc


def is_forex_market_open(
    daily_start_utc: int,
    daily_end_utc: int,
    close_hour_est: int,
    open_hour_est: int,
) -> bool:
    """
    Return True if the forex market is open right now.

    Weekend schedule (all times EST):
      - Friday >= close_hour_est  → closed
      - Saturday (all day)        → closed
      - Sunday  <  open_hour_est  → closed
      - Sunday  >= open_hour_est  → open (market reopens)

    During the week, falls back to the daily UTC hour window.

    weekday(): 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
    """
    now = datetime.now(EST)
    weekday = now.weekday()
    hour_est = now.hour

    if weekday == 5:                                     # Saturday — always closed
        return False
    if weekday == 4 and hour_est >= close_hour_est:      # Friday after close
        return False
    if weekday == 6 and hour_est < open_hour_est:        # Sunday before open
        return False

    # Weekday (or Sunday after open) — apply normal daily UTC window
    return is_market_hours(daily_start_utc, daily_end_utc)


def date_range(days_back: int) -> list[str]:
    """Return [today, today-1, ..., today-(days_back-1)] as ISO strings (UTC)."""
    today = datetime.now(UTC).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days_back)]
