"""Today's API spend, counted from outputs.cost_usd, and the daily cap every paid call respects.

The cap is a setting: api.daily_cap_usd (default 0.25 USD). Functions that take
daily_cap_usd=None use the setting; tests and scripts can pass an explicit value.
"""
import time

from contentpilot.copilot.rules import DAY

DAILY_CAP_KEY = "api.daily_cap_usd"
DEFAULT_DAILY_CAP_USD = 0.25


class DailyCapReached(RuntimeError):
    """Today's API spend has reached the cap; no new call was made."""


def spent_today(conn, now: int | None = None) -> float:
    now = int(time.time()) if now is None else now
    start = now - now % DAY
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM outputs WHERE created_at >= ?", (start,)).fetchone()
    return float(row[0])


def get_daily_cap(conn) -> float:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (DAILY_CAP_KEY,)).fetchone()
    try:
        value = float(row[0]) if row else DEFAULT_DAILY_CAP_USD
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP_USD
    return value if value >= 0 else DEFAULT_DAILY_CAP_USD


def set_daily_cap(conn, value: float) -> None:
    if value < 0:
        raise ValueError("api.daily_cap_usd must be 0 or more")
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (DAILY_CAP_KEY, str(value)))


def resolve_cap(conn, daily_cap_usd: float | None) -> float:
    """An explicit cap wins; None means the api.daily_cap_usd setting."""
    return get_daily_cap(conn) if daily_cap_usd is None else daily_cap_usd


def check_cap(conn, daily_cap_usd: float | None = None, now: int | None = None) -> None:
    cap = resolve_cap(conn, daily_cap_usd)
    spent = spent_today(conn, now)
    if spent >= cap:
        raise DailyCapReached(f"Today's API spend (${spent:.2f}) has reached the ${cap:.2f} cap.")


def log_cost(conn, kind: str, key: str, model: str | None, cost_usd: float | None,
             content: dict | None = None, now: int | None = None, version: str = "spend") -> None:
    """Record a paid call that has no cached output of its own (card chat, extraction)."""
    import json

    now = int(time.time()) if now is None else now
    conn.execute(
        "INSERT OR IGNORE INTO outputs (kind, cache_key, prompt_version, content_json, model, cost_usd, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, key, version, json.dumps(content or {}), model, cost_usd, now),
    )
