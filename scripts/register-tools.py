#!/usr/bin/env python3
"""
register-tools.py — Register the household tool library in Open WebUI.

- Reads each ui/tools/household_*.py file and POSTs it to /api/v1/tools/create
  (id = filename stem) with PUBLIC access grants (user:* read) so every user
  can use them.
- Then sets meta.toolIds on all 6 workspace models so every chat
  auto-enables the tools (frontend reads model.info.meta.toolIds).
- Idempotent: existing tools are updated via /api/v1/tools/id/{id}/update
  (content refresh after edits) and access grants re-applied.
- Requires DATA_DIR + WEBUI_SECRET_KEY (or WEBUI_SECRET_KEY_FILE) env.

Usage:
    DATA_DIR=/Users/mick/Documents/Workspaces/ui/data \
    WEBUI_SECRET_KEY_FILE=/Users/mick/Documents/Workspaces/ui/.webui_secret_key \
    env/bin/python scripts/register-tools.py
"""

import json
import sqlite3
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UI = HERE.parent
TOOLS_DIR = UI / "tools"
DATA_DIR = Path(os.environ.get("DATA_DIR", UI / "data"))
ADMIN_USER_ID = "4800bc91-2e6b-4a41-82db-65777edcbad7"

PUBLIC_GRANTS = [{"principal_type": "user", "principal_id": "*", "permission": "read"}]


def admin_token() -> str:
    if not os.environ.get("WEBUI_SECRET_KEY") and os.environ.get("WEBUI_SECRET_KEY_FILE"):
        key_file = Path(os.environ["WEBUI_SECRET_KEY_FILE"])
        if key_file.exists():
            os.environ["WEBUI_SECRET_KEY"] = key_file.read_text().strip()
    sys.path.insert(0, str(UI / "env/lib/python3.11/site-packages"))
    from open_webui.utils.auth import create_token

    return create_token(data={"id": ADMIN_USER_ID})


def api(base: str, method: str = "GET", payload: dict | None = None, token: str | None = None) -> dict | list:
    url = f"http://127.0.0.1:8390{base}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} {method} {base}: {body[:300]}")


def tool_id_from_file(p: Path) -> str:
    return p.stem.lower()


def main() -> int:
    token = admin_token()
    print(f"admin token OK ({ADMIN_USER_ID})")

    tool_files = sorted(TOOLS_DIR.glob("household_*.py"))
    print(f"found {len(tool_files)} tool modules")

    tool_ids = []
    for tf in tool_files:
        tid = tool_id_from_file(tf)
        tool_ids.append(tid)
        content = tf.read_text("utf-8")
        doc = content.split('"""', 2)
        description = (doc[1].strip().splitlines()[0] if len(doc) > 1 else tid) or tid
        form = {
            "id": tid,
            "name": f"Household · {tid.removeprefix('household_').replace('_', ' ').title()}",
            "content": content,
            "meta": {"description": description},
            "access_grants": PUBLIC_GRANTS,
        }
        # update if exists
        try:
            existing = api(f"/api/v1/tools/id/{tid}", token=token)
            if existing:
                api(f"/api/v1/tools/id/{tid}/update", "POST", form, token)
                print(f"  updated  {tid}")
        except RuntimeError:
            pass
        # create if missing
        try:
            api("/api/v1/tools/create", "POST", form, token)
            print(f"  created  {tid}")
        except RuntimeError as e:
            if "already exists" not in str(e) and "ID_TAKEN" not in str(e):
                print(f"  !! create failed {tid}: {e}")
        # enforce public grants
        try:
            api(
                f"/api/v1/tools/id/{tid}/access/update",
                "POST",
                {"id": tid, "access_grants": PUBLIC_GRANTS},
                token,
            )
        except RuntimeError as e:
            print(f"  !! access grants failed {tid}: {e}")

    # 2) set meta.toolIds on all workspace models
    # NOTE: the models API STRIPS params (system prompt etc.) from responses
    # (routers/models.py read-only sanitize) — read params from the DB so a
    # register-tools run never wipes them (B42 regression: register-tools was
    # silently dropping params.system by sending params={}).
    db_params: dict = {}
    try:
        import os
        db_path = os.path.join(os.environ.get("DATA_DIR", "data"), "webui.db")
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        for mid, pj in con.execute("SELECT id, params FROM model"):
            try:
                p = json.loads(pj or "{}")
            except Exception:
                p = {}
            db_params[mid] = p if isinstance(p, dict) else {}
        con.close()
    except Exception:
        pass

    models = api("/api/v1/models", token=token)
    if isinstance(models, dict):
        models = models.get("data", [])
    updated = 0
    for m in models:
        mid = m.get("id")
        if not mid or mid.startswith(("agentrouter/", "openai/", "vertex/")):
            continue
        # B42: preserve model knowledge + capabilities when rewriting meta —
        # the operative meta lives under info.meta (toolIds/knowledge/caps).
        info_meta = (m.get("info") or {}).get("meta") or {}
        meta = m.get("meta") or {}
        meta["toolIds"] = tool_ids
        if info_meta.get("knowledge"):
            meta["knowledge"] = info_meta["knowledge"]
        if info_meta.get("capabilities"):
            meta["capabilities"] = info_meta["capabilities"]
        if info_meta.get("builtinTools"):
            meta["builtinTools"] = info_meta["builtinTools"]
        form = {
            "id": mid,
            "name": m.get("name", mid),
            "meta": meta,
            "params": db_params.get(mid) or m.get("params") or {},
            "access_grants": PUBLIC_GRANTS,
        }
        try:
            api("/api/v1/models/model/update", "POST", form, token)
            print(f"  model {mid}: toolIds -> {len(tool_ids)} tools")
            updated += 1
        except RuntimeError as e:
            print(f"  !! model update failed {mid}: {e}")

    print(f"done: {len(tool_ids)} tools, {updated} models updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
