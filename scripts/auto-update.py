#!/usr/bin/env python3
"""
Auto-update — Chat·hector.app (Phase 5)

Runs daily (01:00 UTC) as a launchd user agent via the OWUI venv python
(TCC-approved binary — see watchdog.py header for why). NO popups, silent:

  1. Read pinned version from .owui-version
  2. Check PyPI for the newest open-webui release
  3. If newer: backup (pip freeze + DB), upgrade in venv,
     RE-RUN the sub-overflow patch (CRITICAL — an upgrade replaces
     models/users.py and re-breaks Google login; README G13),
     restart the OWUI launchd agent, smoke-test (health + chat + reasoning),
     on ANY failure: roll back to the pinned version and restart.
  4. Writes logs to logs/auto-update.log (silent unless failure).
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

UI_DIR = pathlib.Path("/Users/mick/Documents/Workspaces/ui")
DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", str(UI_DIR / "data")))
LOG_FILE = UI_DIR / "logs" / "auto-update.log"
PIN_FILE = UI_DIR / ".owui-version"
ENV_PY = UI_DIR / "env" / "bin" / "python"
BRIDGE_KEY = ""

LAUNCHD_LABEL = f"gui/{os.getuid()}/org.hache.chat.openwebui"


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)


def run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except Exception as e:
        return 1, str(e)


def main() -> int:
    log("auto-update start")
    if not PIN_FILE.exists():
        log("ABORT: no pinned version (.owui-version missing)")
        return 1
    pinned = PIN_FILE.read_text().strip()
    if not pinned:
        log("ABORT: empty pinned version")
        return 1

    # find latest on PyPI
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/open-webui/json", timeout=30) as r:
            latest = json.load(r)["info"]["version"]
    except Exception as e:
        log(f"SKIP: PyPI unreachable ({e})")
        return 0

    if pinned == latest:
        log(f"no update ({pinned})")
        return 0

    log(f"UPDATE {pinned} -> {latest}")

    # backup current state
    bk = DATA_DIR / "backups" / f"pre-upgrade-{pinned}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    bk.mkdir(parents=True, exist_ok=True)
    # venv has no pip module — use uv (Homebrew) with --python pointing at the venv
    UV = "/opt/homebrew/bin/uv"
    rc0, freeze_out = run([UV, "pip", "freeze", "--python", str(ENV_PY)], timeout=120)
    if rc0 == 0:
        with open(bk / "requirements.txt", "w") as f:
            f.write(freeze_out)
    run(["sqlite3", str(DATA_DIR / "webui.db"), f"VACUUM INTO '{bk / 'webui.db'}'"], timeout=120)
    log(f"backup -> {bk}")

    # upgrade
    rc, out = run([UV, "pip", "install", "--python", str(ENV_PY), "--quiet", "--upgrade", f"open-webui=={latest}"], timeout=600)
    if rc != 0:
        log(f"FAIL: pip upgrade error: {out[-300:]}")
        return 1

    # CRITICAL: re-apply Google-sub overflow patch
    rc, out = run(["bash", str(UI_DIR / "scripts" / "patch-owui-sub-overflow.sh")], timeout=60)
    if rc != 0:
        log(f"WARN: patch re-apply rc={rc}: {out[-200:]}")

    # restart OWUI agent
    run(["launchctl", "kickstart", "-k", LAUNCHD_LABEL], timeout=30)
    time.sleep(16)

    # smoke tests
    smoke_ok = True
    try:
        with urllib.request.urlopen("http://127.0.0.1:8390/health", timeout=10) as r:
            smoke_ok = smoke_ok and r.status == 200
    except Exception:
        smoke_ok = False

    # full chat via bridge
    try:
        env_file = UI_DIR / ".env"
        key = ""
        for ln in env_file.read_text().splitlines():
            if ln.startswith("BRIDGE_API_KEY="):
                key = ln.split("=", 1)[1].strip()
        req = urllib.request.Request(
            "http://127.0.0.1:8484/v1/chat/completions",
            data=json.dumps({
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "Say OK"}],
                "max_tokens": 10,
            }).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
            smoke_ok = smoke_ok and '"content"' in json.dumps(body.get("choices", [{}])[0].get("message", {}))
    except Exception:
        smoke_ok = False

    if not smoke_ok:
        log(f"FAIL: smoke test — rolling back to {pinned}")
        run([UV, "pip", "install", "--python", str(ENV_PY), "--quiet", f"open-webui=={pinned}"], timeout=600)
        run(["bash", str(UI_DIR / "scripts" / "patch-owui-sub-overflow.sh")], timeout=60)
        run(["launchctl", "kickstart", "-k", LAUNCHD_LABEL], timeout=30)
        time.sleep(16)
        log(f"rolled back to {pinned}")
        return 1

    PIN_FILE.write_text(latest)
    log(f"OK: upgraded + verified ({latest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
