"""scan_and_push (laptop) never touches a database; backup_db snapshots and keeps 7 days."""
import os
import sqlite3
import time

import pytest


class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self.body


def test_scan_and_push_posts_batches_and_opens_no_database(monkeypatch, tmp_path):
    from contentpilot import config
    from scripts import scan_and_push
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(scan_and_push, "LOG_PATH", str(tmp_path / "scan_and_push.log"))
    monkeypatch.setattr(scan_and_push, "load_dotenv", lambda: None)
    monkeypatch.setenv("CONTENTPILOT_VM_URL", "https://vm.example.ts.net/")
    monkeypatch.setenv("INGEST_TOKEN", "secret-token")
    monkeypatch.setattr(sqlite3, "connect", sqlite3.connect)     # restored after the test by monkeypatch

    urls = [f"https://www.linkedin.com/posts/p-{i}" for i in range(23)]
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers))
        with pytest.raises(scan_and_push.LocalDatabaseRefused):
            sqlite3.connect(":memory:")                            # any DB access is refused mid-run
        return _Resp({"counts": {"added": len(json["urls"])},
                      "results": [{"url": u, "status": "added", "title": "t"} for u in json["urls"]]})

    code = scan_and_push.main([], scrape=lambda time_window=None, headless=False: urls, post=fake_post)
    assert code == 0
    assert [len(j["urls"]) for _, j, _ in sent] == [10, 10, 3]
    assert all(u == "https://vm.example.ts.net/api/ingest" and h == {"X-Ingest-Token": "secret-token"}
               for u, _, h in sent)
    log = (tmp_path / "scan_and_push.log").read_text(encoding="utf-8")
    assert "scan found 23" in log and "pushed to VM: added 23" in log


def test_scan_and_push_needs_config_and_reports_bad_token(monkeypatch, tmp_path):
    from contentpilot import config
    from scripts import scan_and_push
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(scan_and_push, "LOG_PATH", str(tmp_path / "log"))
    monkeypatch.setattr(scan_and_push, "load_dotenv", lambda: None)
    monkeypatch.setattr(sqlite3, "connect", sqlite3.connect)
    monkeypatch.delenv("CONTENTPILOT_VM_URL", raising=False)
    monkeypatch.setenv("INGEST_TOKEN", "x")
    assert scan_and_push.main([], scrape=lambda **k: ["https://a"]) == 2

    monkeypatch.setenv("CONTENTPILOT_VM_URL", "https://vm")
    assert scan_and_push.main([], scrape=lambda **k: ["https://a"],
                              post=lambda *a, **k: _Resp({}, status=401)) == 1
    assert "rejected INGEST_TOKEN" in (tmp_path / "log").read_text(encoding="utf-8")


def test_backup_snapshot_and_seven_day_retention(tmp_path):
    from scripts.backup_db import backup
    src = tmp_path / "live.sqlite3"
    c = sqlite3.connect(src)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (x)")
    c.execute("INSERT INTO t VALUES (42)")
    c.commit()                                                     # still open, like the running app
    dest = tmp_path / "backups"
    dest.mkdir()
    old = dest / "contentpilot-20260101-033000.sqlite3"
    recent = dest / "contentpilot-20261120-033000.sqlite3"
    for p, age_days in ((old, 9), (recent, 3)):
        p.write_bytes(b"x")
        os.utime(p, (time.time() - age_days * 86_400,) * 2)

    target, removed = backup(str(src), str(dest), keep_days=7)
    c.close()
    assert removed == [str(old)] and recent.exists()
    b = sqlite3.connect(target)
    assert b.execute("SELECT x FROM t").fetchone()[0] == 42
    b.close()
