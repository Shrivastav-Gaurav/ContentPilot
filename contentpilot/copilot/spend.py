"""Today's API spend, counted from outputs.cost_usd. Shared by every module that calls a paid API."""
import time

from contentpilot.copilot.rules import DAY

DEFAULT_DAILY_CAP_USD = 0.10


class DailyCapReached(RuntimeError):
    """Today's API spend has reached the cap; no new call was made."""


def spent_today(conn, now: int | None = None) -> float:
    now = int(time.time()) if now is None else now
    start = now - now % DAY
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM outputs WHERE created_at >= ?", (start,)).fetchone()
    return float(row[0])


def check_cap(conn, daily_cap_usd: float = DEFAULT_DAILY_CAP_USD, now: int | None = None) -> None:
    spent = spent_today(conn, now)
    if spent >= daily_cap_usd:
        raise DailyCapReached(f"Today's API spend (${spent:.2f}) has reached the ${daily_cap_usd:.2f} cap.")
