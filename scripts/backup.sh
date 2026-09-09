#!/bin/bash
# ============================================================================
# Delta backup — Chat·hector.app (Phase 5)
#
# Snapshots the Open WebUI SQLite DB via VACUUM INTO (consistent, online-safe)
# into <DATA_DIR>/backups/webui-YYYYMMDD-HHMMSS.db, keeps the last 7 files,
# prunes older ones. Also snapshots the telemetry dir.
# ============================================================================
set -u

DATA_DIR="${DATA_DIR:-/Users/mick/Documents/Workspaces/ui/data}"
BACKUP_DIR="$DATA_DIR/backups"
mkdir -p "$BACKUP_DIR"

STAMP=$(date +%Y%m%d-%H%M%S)
DB="$DATA_DIR/webui.db"
OUT="$BACKUP_DIR/webui-$STAMP.db"

# VACUUM INTO requires a writable destination; SQLite creates it atomically.
/usr/bin/sqlite3 "$DB" "VACUUM INTO '$OUT'" 2>/dev/null || {
  echo "ERROR: VACUUM INTO failed" >> "$BACKUP_DIR/backup.log"
  exit 1
}

# Telemetry snapshot (cheap, tiny)
if [ -d "$DATA_DIR/telemetry" ]; then
  tar -czf "$BACKUP_DIR/telemetry-$STAMP.tgz" -C "$DATA_DIR" telemetry 2>/dev/null
fi

# Retention: keep 7 newest webui-*.db, 7 telemetry-*.tgz
ls -1t "$BACKUP_DIR"/webui-*.db 2>/dev/null | tail -n +8 | xargs -r rm -f
ls -1t "$BACKUP_DIR"/telemetry-*.tgz 2>/dev/null | tail -n +8 | xargs -r rm -f

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup OK $OUT" >> "$BACKUP_DIR/backup.log"
echo "backup: $OUT ($(du -h "$OUT" | cut -f1))"
echo "retained: $(ls -1 "$BACKUP_DIR"/webui-*.db 2>/dev/null | wc -l | tr -d ' ') dbs"
exit 0
