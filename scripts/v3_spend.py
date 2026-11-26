"""API spend from the outputs table, today and all time, by kind and model,
plus the model each feature is configured to use.

    python -m scripts.v3_spend

Read only. "Today" uses the same UTC day boundary as spent_today() and the daily cap.
"""
import time

from dotenv import load_dotenv

from contentpilot import config
from contentpilot.copilot.rules import DAY
from contentpilot.copilot.spend import get_daily_cap, spent_today
from contentpilot.db import get_connection


def _table(conn, since: int | None) -> None:
    sql = ("SELECT kind, COALESCE(model, '(none)') AS model, COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS cost, "
           "SUM(cost_usd IS NULL) AS unpriced FROM outputs")
    params: tuple = ()
    if since is not None:
        sql += " WHERE created_at >= ?"
        params = (since,)
    rows = conn.execute(sql + " GROUP BY kind, model ORDER BY cost DESC, kind", params).fetchall()
    if not rows:
        print("  (nothing)")
        return
    w_kind = max(len("kind"), *(len(r["kind"]) for r in rows))
    w_model = max(len("model"), *(len(r["model"]) for r in rows))
    print(f"  {'kind':<{w_kind}}  {'model':<{w_model}}  {'count':>6}  {'cost_usd':>10}")
    for r in rows:
        note = f"  ({r['unpriced']} without a cost)" if r["unpriced"] else ""
        print(f"  {r['kind']:<{w_kind}}  {r['model']:<{w_model}}  {r['n']:>6}  {r['cost']:>10.5f}{note}")
    total_n = sum(r["n"] for r in rows)
    total = sum(r["cost"] for r in rows)
    print(f"  {'TOTAL':<{w_kind}}  {'':<{w_model}}  {total_n:>6}  {total:>10.5f}")


def main() -> None:
    load_dotenv()
    now = int(time.time())
    today = now - now % DAY
    conn = get_connection()
    try:
        print(f"Today (UTC, since {time.strftime('%Y-%m-%d %H:%M', time.gmtime(today))})")
        _table(conn, today)
        print("\nAll time")
        _table(conn, None)
        cap, spent = get_daily_cap(conn), spent_today(conn, now)
    finally:
        conn.close()

    haiku, sonnet = config.openrouter_model(), config.synthesis_model()
    print("\nConfigured models")
    for feature, model, source in (
        ("triage", haiku, "OPENROUTER_MODEL"),
        ("fit", haiku, "OPENROUTER_MODEL"),
        ("prep_note", haiku, "OPENROUTER_MODEL"),
        ("synthesizer", haiku, "OPENROUTER_MODEL"),
        ("deeper answer", sonnet, "SYNTHESIS_MODEL, only when clicked"),
        ("extraction", haiku, "OPENROUTER_MODEL"),
        ("card chat", sonnet, "SYNTHESIS_MODEL"),
        ("embeddings", config.EMBEDDING_MODEL, "fixed in config.py"),
    ):
        print(f"  {feature:<14} {model:<32} ({source})")
    print(f"\nDaily cap (api.daily_cap_usd): ${cap:.2f} | spent today ${spent:.4f}")
    print("Note: past extraction costs were copied in from cards (migration 8). Card chats before this\n"
          "change recorded no cost, so they are missing from the totals.")


if __name__ == "__main__":
    main()
