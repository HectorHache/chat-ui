#!/usr/bin/env python3
"""
Delta backup — Chat·hector.app (Phase 5)

Snapshots the Open WebUI SQLite DB via VACUUM INTO (consistent, online-safe)
into <DATA_DIR>/backups/webui-YYYYMMDD-HHMMSS.db, keeps the last 7 files,
prunes older ones. Also snapshots the telemetry dir.

Runs as a launchd user agent via the OWUI venv python (TCC-approved binary —
see watchdog.py header for why).
"""
import os
import pathlib
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone

DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "/Users/mick/Documents/Workspaces/ui/data"))
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = BACKUP_DIR / "backup.log"


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    db = DATA_DIR / "webui.db"
    out = BACKUP_DIR / f"webui-{stamp}.db"

    try:
        conn = sqlite3.connect(str(db), timeout=10)
        try:
            conn.execute(f"VACUUM INTO '{out}'")
        finally:
            conn.close()
    except Exception as e:
        log(f"ERROR VACUUM INTO failed: {e}")
        return 1

    # telemetry snapshot (cheap, tiny)
    tel = DATA_DIR / "telemetry"
    if tel.is_dir():
        tgz = BACKUP_DIR / f"telemetry-{stamp}.tgz"
        try:
            with tarfile.open(tgz, "w:gz") as tf:
                tf.add(tel, arcname="telemetry")
        except Exception as e:
            log(f"WARN telemetry snapshot failed: {e}")

    # retention: keep 7 newest webui-*.db and telemetry-*.tgz
    for pattern in ("webui-*.db", "telemetry-*.tgz"):
        files = sorted(BACKUP_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[7:]:
            old.unlink(missing_ok=True)

    log(f"backup OK {out.name} ({out.stat().st_size} bytes)")
    print(f"backup: {out} ({out.stat().st_size} bytes)")
    print(f"retained: {len(list(BACKUP_DIR.glob('webui-*.db')))} dbs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
