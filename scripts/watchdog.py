#!/usr/bin/env python3
"""
Watchdog — Chat·hector.app (Phase 5, B24; Phase 6 reminders)

Runs as a launchd user agent via the OWUI venv python (TCC-approved binary —
plain /bin/bash and /usr/bin/python3 are DENIED ~/Documents access under
launchd; the venv interpreter OWUI itself runs as has full access, verified
2026-08-31).

Probes the REAL endpoints (not process listings — root daemons are invisible
to non-root sandboxes; B24) and writes a status file + alert log.

Checks:
  1. https://chat.hector.app/health        (Caddy TLS + OWUI, tailnet)
  2. https://localhost:8383/health        (Caddy TLS + OWUI, local)
  3. http://127.0.0.1:8484/health         (model bridge: AR + Vertex)
  4. Credit-expiry warning windows (2026-11-15..22 AgentRouter;
     2026-11-24..30 Vertex v2 free budget) — once/day each
  5. Due household reminders (data/reminders/*.json, Phase 6) — mark
     delivered + macOS notification (admin machine)
  6. Vertex provider status from bridge /health (vertex1/vertex2/active)

Outputs:
  <DATA_DIR>/watchdog/status.json    — latest check (read by anything)
  <DATA_DIR>/watchdog/alerts.log     — append-only alert trail

Notifications: macOS notification center via osascript (best-effort, only on
transition healthy->unhealthy, credit-warning day, or due reminder). No
external deps.
"""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "/Users/mick/Documents/Workspaces/ui/data"))
WD_DIR = DATA_DIR / "watchdog"
UI_DIR = pathlib.Path("/Users/mick/Documents/Workspaces/ui")
WD_DIR.mkdir(parents=True, exist_ok=True)
STATUS_FILE = WD_DIR / "status.json"
ALERT_LOG = WD_DIR / "alerts.log"
PREV_FILE = WD_DIR / "previous-overall"

CHAT_HEALTH = "https://chat.hector.app/health"
LOCAL_HEALTH = "https://localhost:8383/health"
BRIDGE_HEALTH = "http://127.0.0.1:8484/health"
WHISPER_HEALTH = "http://127.0.0.1:8499/health"  # local STT worker (B40)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe(url: str, timeout: int = 8, headers: dict | None = None, verify: bool = True) -> bool:
    import ssl

    ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status == 200
    except Exception:
        return False


def notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def log_alert(line: str) -> None:
    with open(ALERT_LOG, "a") as f:
        f.write(f"{now_iso()} {line}\n")


def check_reminders() -> int:
    """Mark due household reminders as delivered and notify locally (Phase 6).

    Reads <DATA_DIR>/reminders/<user_id>.json (written by the
    household_reminders tool). Due items get status=delivered and a macOS
    notification (admin machine) + alert log line. Returns the number of
    reminders delivered this run.
    """
    rem_dir = DATA_DIR / "reminders"
    if not rem_dir.exists():
        return 0
    now = time.time()
    delivered = 0
    for f in sorted(rem_dir.glob("*.json")):
        try:
            items = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        changed = False
        for item in items:
            if item.get("status") == "pending" and float(item.get("due_at", 0)) <= now:
                item["status"] = "delivered"
                item["delivered_at"] = now
                changed = True
                delivered += 1
                text = str(item.get("text", "reminder"))
                user = f.stem
                log_alert(f"REMINDER due ({user}): {text}")
                notify("Chat·hector reminder", f"{text} (user: {user})")
        if changed:
            f.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")
    return delivered


def check_prices() -> int:
    """Check active price watches (Phase 6).

    Reads <DATA_DIR>/prices/<user_id>.json (written by household_prices).
    For each active watch, fetches the URL and extracts the first
    €/price pattern; if price <= target and not yet notified, marks
    notified and returns a count. Fragile by design (regex on arbitrary
    product pages) — surfaced as best-effort alerts.
    """
    prices_dir = DATA_DIR / "prices"
    if not prices_dir.exists():
        return 0
    alerts = 0
    for f in sorted(prices_dir.glob("*.json")):
        try:
            items = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        changed = False
        for item in items:
            if not item.get("active") or item.get("notified"):
                continue
            url = str(item.get("url", ""))
            if not url.startswith(("http://", "https://")):
                continue
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                        "Accept-Language": "en,de;q=0.8,nl;q=0.7",
                    },
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    html = resp.read(300_000).decode("utf-8", errors="replace")
            except Exception:
                continue
            m = None
            for pat in (r"[\u20ac]\s*([0-9]+[.,][0-9]{2})", r"([0-9]+[.,][0-9]{2})\s*[\u20ac]", r"\$\s*([0-9]+[.,][0-9]{2})", r"\"?price\"?\s*:\s*([0-9]+[.,][0-9]{2})"):
                m = m or __import__("re").search(pat, html)
            if not m:
                continue
            price = float(m.group(1).replace(",", "."))
            item["last_price"] = price
            item["last_check"] = time.time()
            if price <= float(item.get("target_price", 0)):
                item["notified"] = True
                changed = True
                alerts += 1
                name = str(item.get("name", url))
                user = f.stem
                log_alert(f"PRICE-ALERT ({user}): {name} now {price:.2f} (target <= {item.get('target_price')})")
                notify("Chat·hector price watch", f"{name}: now {price:.2f} (target <= {item.get('target_price')})")
            elif item.get("last_price") != price:
                changed = True
        if changed:
            f.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")
    return alerts


def main() -> int:
    chat_ok = probe(CHAT_HEALTH)
    local_ok = probe(LOCAL_HEALTH, verify=False)  # mkcert CA not in certifi; loopback, TLS verify irrelevant

    # bridge requires auth
    bridge_key = ""
    env_file = UI_DIR / ".env"
    try:
        for ln in env_file.read_text().splitlines():
            if ln.startswith("BRIDGE_API_KEY="):
                bridge_key = ln.split("=", 1)[1].strip()
                break
    except Exception:
        pass
    bridge_ok = probe(BRIDGE_HEALTH, headers={"Authorization": f"Bearer {bridge_key}"})

    overall = "ok" if (chat_ok and local_ok and bridge_ok) else "degraded"

    # credit-expiry warnings (once per day each)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    credit_warn = 0
    credit_windows = [
        ("2026-11-15", "2026-11-22", "credit-warn-ar",
         "AgentRouter credits expire 2026-11-22 — top up soon (daily reminder)."),
        ("2026-11-24", "2026-11-30", "credit-warn-vertex2",
         "Vertex v2 free budget (263.35 EUR) expires 2026-11-30 — check alternatives daily."),
    ]
    for wstart, wend, wname, wmsg in credit_windows:
        if wstart <= today <= wend:
            marker = WD_DIR / f"{wname}-{today}.marker"
            if not marker.exists():
                credit_warn = 1
                marker.touch()
                log_alert(f"CREDIT-WARN {wmsg}")
                notify("Chat·hector credits", wmsg)

    status = {
        "checked_at": now_iso(),
        "overall": overall,
        "chat_hector_app": int(chat_ok),
        "localhost_8383": int(local_ok),
        "bridge_8484": int(bridge_ok),
        "credit_warning_active": credit_warn,
    }
    # vertex provider detail from bridge /health (v1 primary, v2 failover)
    try:
        req = urllib.request.Request(
            BRIDGE_HEALTH, headers={"Authorization": f"Bearer {bridge_key}"}
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            bj = json.loads(r.read().decode())
        status["stt_mode"] = bj.get("stt", "unknown")  # groq+local | local-only (B40)
        status["vertex1"] = bj.get("vertex1", "unknown")
        status["vertex2"] = bj.get("vertex2", "unknown")
        status["vertex_active"] = bj.get("vertexActive")
    except Exception:
        status["vertex1"] = status["vertex2"] = "unknown"
        status["vertex_active"] = None
        status["stt_mode"] = "unknown"
    try:
        with urllib.request.urlopen(WHISPER_HEALTH, timeout=5) as wr:
            status["whisper_8499"] = int(wr.status == 200)
    except Exception:
        status["whisper_8499"] = 0
    try:
        status["reminders_due"] = check_reminders()
    except Exception:
        status["reminders_due"] = 0
    try:
        status["price_alerts"] = check_prices()
    except Exception:
        status["price_alerts"] = 0
    STATUS_FILE.write_text(json.dumps(status, indent=2), encoding="utf-8")

    # alert on healthy -> degraded transition
    if overall == "degraded":
        prev = PREV_FILE.read_text().strip() if PREV_FILE.exists() else "ok"
        if prev != "degraded":
            msg = f"chat.hector.app={int(chat_ok)} localhost:8383={int(local_ok)} bridge:8484={int(bridge_ok)}"
            log_alert(f"DEGRADED {msg}")
            notify("Chat·hector watchdog", f"Service degraded: {msg}")
    PREV_FILE.write_text(overall)

    return 0


if __name__ == "__main__":
    sys.exit(main())
