"""Online SQLite backup (same as sqlite3 .backup) with 7-day retention. Run nightly on the VM.

    python -m scripts.backup_db                       # data/contentpilot.sqlite3 -> ~/contentpilot-backups
    python -m scripts.backup_db --dest DIR --keep-days 7

Safe while the app is running (SQLite's backup API copies a consistent snapshot).
Restore: see deploy/README-vm.md.
"""
import argparse
import glob
import os
import sqlite3
import time

from contentpilot import config

PREFIX = "contentpilot-"


def backup(src: str, dest_dir: str, keep_days: int = 7, now: float | None = None) -> tuple[str, list[str]]:
    now = time.time() if now is None else now
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, f"{PREFIX}{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}.sqlite3")
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
            ok = dest.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                raise RuntimeError(f"backup failed integrity_check: {ok}")
        finally:
            dest.close()
    finally:
        source.close()
    removed = []
    for path in glob.glob(os.path.join(dest_dir, f"{PREFIX}*.sqlite3")):
        if path != target and now - os.path.getmtime(path) > keep_days * 86_400:
            os.remove(path)
            removed.append(path)
    return target, removed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default=config.DB_PATH)
    parser.add_argument("--dest", default=os.path.expanduser("~/contentpilot-backups"))
    parser.add_argument("--keep-days", type=int, default=7)
    args = parser.parse_args()
    target, removed = backup(args.src, args.dest, args.keep_days)
    print(f"backup: {target} ({os.path.getsize(target):,} bytes); removed {len(removed)} older than {args.keep_days} days")


if __name__ == "__main__":
    main()
