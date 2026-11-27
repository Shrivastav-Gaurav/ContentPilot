"""The deploy files agree with each other: unit ports = env ports = `tailscale serve` targets,
and every API route is under a path that `tailscale serve` publishes."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env() -> dict:
    out = {}
    for line in (ROOT / "deploy/contentpilot.env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _serve_commands() -> list[str]:
    readme = (ROOT / "deploy/README-vm.md").read_text(encoding="utf-8")
    return [l for l in readme.splitlines() if "tailscale serve --bg" in l]


def test_ports_match_between_env_units_and_serve():
    env = _env()
    web, api = env["STREAMLIT_SERVER_PORT"], env["CP_API_PORT"]
    assert web == "8502" and api == "8601"

    web_unit = (ROOT / "deploy/systemd/contentpilot-web.service").read_text(encoding="utf-8")
    api_unit = (ROOT / "deploy/systemd/contentpilot-api.service").read_text(encoding="utf-8")
    assert "--server.address 127.0.0.1" in web_unit and "--server.port" not in web_unit   # port from env
    assert "--host 127.0.0.1 --port ${CP_API_PORT}" in api_unit

    serve = _serve_commands()
    assert serve, "README-vm.md must show the tailscale serve commands"
    targets = set(re.findall(r"http://127\.0\.0\.1:(\d+)", "\n".join(serve)))
    assert targets == {web, api}
    assert not any("funnel" in l for l in serve)


def test_every_api_route_is_published_by_serve():
    from contentpilot.api import _MOUNTED, app
    serve = "\n".join(_serve_commands())
    for prefix in _MOUNTED:
        assert prefix in serve, f"{prefix} is not in the tailscale serve commands"
    for route in app.routes:
        if hasattr(route, "methods"):
            assert route.path.startswith(_MOUNTED), f"{route.path} would not be reachable through tailscale serve"


def test_units_follow_postpilot_conventions_and_limit_memory():
    for name in ("web", "api", "worker"):
        unit = (ROOT / f"deploy/systemd/contentpilot-{name}.service").read_text(encoding="utf-8")
        for line in ("Restart=always", "RestartSec=10", "NoNewPrivileges=true", "PrivateTmp=true",
                     "User=@USER@", "OOMScoreAdjust=500"):
            assert line in unit, f"contentpilot-{name}: missing {line}"
        assert re.search(r"^MemoryMax=\d+M$", unit, re.M), f"contentpilot-{name}: no MemoryMax"


def test_no_secrets_or_personal_files_tracked():
    import subprocess
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    bad = [f for f in tracked if re.search(r"(^|/)\.env$|\.docx?$|(^|/)notes\.md$|\.sqlite3?$|\.db$|"
                                           r"linkedin-session|deploy/contentpilot\.env$", f)]
    assert bad == []
