"""
household_status — System status (admin only).

"Everything healthy?" — reads the watchdog status file and probes the
local endpoints. Only the admin user may invoke this.
"""

import json
import os
import shutil
import urllib.request
from pathlib import Path

_DATA_DIR = Path(os.environ.get("DATA_DIR") or (Path.home() / "Documents/Workspaces/ui/data"))


class Tools:
    async def system_status(self, __user__: dict | None = None) -> str:
        """
        Report the health of the chat service (admin only).

        Use when the user asks "is everything healthy", "system status",
        "is the server ok", or reports something not working.

        :return: A health report: endpoints, watchdog state, disk space.
        """
        role = (__user__ or {}).get("role", "")
        if role != "admin":
            return "This tool is restricted to the administrator. Politely decline for other users."

        lines = []

        # 1) watchdog status file
        status_path = _DATA_DIR / "watchdog" / "status.json"
        try:
            if status_path.exists():
                st = json.loads(status_path.read_text("utf-8"))
                overall = st.get("overall", "unknown")
                checked = st.get("checked_at", "?")
                lines.append(f"Watchdog: **{overall}** (checked {checked})")
                for key in ("chat_hector_app", "localhost_8383", "bridge_8484"):
                    v = st.get(key)
                    if v is not None:
                        lines.append(f"- {key}: {'ok' if v else 'DOWN'}")
            else:
                lines.append("Watchdog: no status file yet")
        except Exception as e:
            lines.append(f"Watchdog: unreadable ({e})")

        # 2) live probes
        probes = [
            ("https://chat.hector.app/health", "Public URL (chat.hector.app)"),
            ("http://127.0.0.1:8390/health", "Internal engine (127.0.0.1:8390)"),
        ]
        for url, label in probes:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Chat-hache-household/1.0"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    ok = resp.status == 200
                lines.append(f"- {label}: {'OK' if ok else 'FAIL'} (HTTP {resp.status})")
            except Exception as e:
                lines.append(f"- {label}: FAIL ({e})")

        # 3) disk
        try:
            usage = shutil.disk_usage(str(_DATA_DIR))
            free_gb = usage.free / (1024**3)
            lines.append(f"Disk free: {free_gb:.1f} GB")
        except Exception:
            pass

        return "\n".join(lines)
