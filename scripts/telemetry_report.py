#!/usr/bin/env python3
"""
Telemetry Aggregator — Chat·hector.app (Phase 4, R19–R22)

Reads every per-user SQLite DB in <DATA_DIR>/telemetry/*.db (written by the
household_telemetry filter) and produces:

  - <DATA_DIR>/telemetry/report.json  — machine-readable lifetime stats
  - <DATA_DIR>/telemetry/report.html  — human-readable lifetime + per-user
                                          breakdown with SVG sparkline

Cost model (per 1M tokens, input/output) — approximate list prices; edit the
PRICING table below if a provider changes rates. Tokens recorded are what the
model bridge reported (usage JSON), NOT charged-credit-equivalents.

Run:  env/bin/python scripts/telemetry_report.py   (DATA_DIR from .env or default)
"""
import json
import os
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone

# ---- config -------------------------------------------------------------
# USD per 1M tokens (input, output). Approximate; edit freely.
PRICING = {
    "deepseek-v4-flash":   (0.14, 0.28),   # cheap workhorse (AR)
    "claude-opus-4-8":     (5.00, 25.00),  # thinking heavy-lifter (AR)
    "claude-opus-5":       (5.00, 25.00),
    "gpt-5.6-sol":         (3.50, 10.00),  # reasoning (AR)
    "glm-5.3":             (2.00, 8.00),
    "gemini-3.7-flash":    (0.50, 3.00),   # Vertex fallback
}
DEFAULT_PRICE = (2.00, 8.00)

DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "data"))
TELEMETRY_DIR = DATA_DIR / "telemetry"


def collect() -> list[dict]:
    rows = []
    for db_file in sorted(TELEMETRY_DIR.glob("*.db")):
        user = db_file.stem  # lowercase user name = filename stem
        conn = sqlite3.connect(str(db_file), timeout=5)
        try:
            cur = conn.execute(
                "SELECT ts, model, chat_id, message_id,"
                "       input_tokens, output_tokens, total_tokens"
                " FROM usage_log ORDER BY ts"
            )
            for ts, model, chat_id, mid, itok, otok, tot in cur.fetchall():
                rows.append(
                    {
                        "user": user,
                        "ts": ts,
                        "model": model or "unknown",
                        "chat_id": chat_id or "",
                        "message_id": mid or "",
                        "input_tokens": int(itok or 0),
                        "output_tokens": int(otok or 0),
                        "total_tokens": int(tot or 0),
                    }
                )
        finally:
            conn.close()
    return rows


def cost_for(model: str, itok: int, otok: int) -> float:
    pi, po = PRICING.get(model, DEFAULT_PRICE)
    return (itok / 1_000_000) * pi + (otok / 1_000_000) * po


def aggregate(rows: list[dict]) -> dict:
    users: dict[str, dict] = {}
    models: dict[str, dict] = {}
    total = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
    first_ts, last_ts = None, None

    for r in rows:
        if first_ts is None or r["ts"] < first_ts:
            first_ts = r["ts"]
        if last_ts is None or r["ts"] > last_ts:
            last_ts = r["ts"]

        cost = cost_for(r["model"], r["input_tokens"], r["output_tokens"])

        u = users.setdefault(
            r["user"],
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
        )
        u["calls"] += 1
        u["input_tokens"] += r["input_tokens"]
        u["output_tokens"] += r["output_tokens"]
        u["total_tokens"] += r["total_tokens"]
        u["cost_usd"] += cost

        m = models.setdefault(
            r["model"],
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
        )
        m["calls"] += 1
        m["input_tokens"] += r["input_tokens"]
        m["output_tokens"] += r["output_tokens"]
        m["total_tokens"] += r["total_tokens"]
        m["cost_usd"] += cost

        total["calls"] += 1
        total["input_tokens"] += r["input_tokens"]
        total["output_tokens"] += r["output_tokens"]
        total["total_tokens"] += r["total_tokens"]
        total["cost_usd"] += cost

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "first_usage_at": datetime.fromtimestamp(first_ts, timezone.utc).isoformat() if first_ts else None,
        "last_usage_at": datetime.fromtimestamp(last_ts, timezone.utc).isoformat() if last_ts else None,
        "total": total,
        "per_user": users,
        "per_model": models,
    }


def sparkline_svg(values: list[float], width=600, height=80) -> str:
    if len(values) < 2 or max(values) <= 0:
        return f'<svg width="{width}" height="{height}"></svg>'
    vmax = max(values)
    step = width / (len(values) - 1)
    pts = []
    for i, v in enumerate(values):
        x = i * step
        y = height - 4 - (v / vmax) * (height - 12)
        pts.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="#4f8cff" stroke-width="2"/>'
        f"</svg>"
    )


def render_html(agg: dict) -> str:
    total = agg["total"]
    def fmt_tokens(n: int) -> str:
        if n >= 1_000_000:
            return f"{n/1_000_000:.2f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}k"
        return str(n)

    # daily series for sparkline (last 30 days)
    import calendar
    day_counts: dict[str, int] = {}
    conns = list(TELEMETRY_DIR.glob("*.db"))
    for db_file in conns:
        conn = sqlite3.connect(str(db_file), timeout=5)
        try:
            for (ts,) in conn.execute("SELECT ts FROM usage_log"):
                day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
                day_counts[day] = day_counts.get(day, 0) + 1
        finally:
            conn.close()
    series = [v for _, v in sorted(day_counts.items())[-30:]]

    rows_user = "".join(
        f"<tr><td>{u}</td><td>{d['calls']}</td><td>{fmt_tokens(d['input_tokens'])}</td>"
        f"<td>{fmt_tokens(d['output_tokens'])}</td><td>{fmt_tokens(d['total_tokens'])}</td>"
        f"<td>${d['cost_usd']:.4f}</td></tr>"
        for u, d in sorted(agg["per_user"].items())
    )
    rows_model = "".join(
        f"<tr><td>{m}</td><td>{d['calls']}</td><td>{fmt_tokens(d['input_tokens'])}</td>"
        f"<td>{fmt_tokens(d['output_tokens'])}</td><td>{fmt_tokens(d['total_tokens'])}</td>"
        f"<td>${d['cost_usd']:.4f}</td></tr>"
        for m, d in sorted(agg["per_model"].items(), key=lambda kv: -kv[1]["total_tokens"])
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Chat·hache — Usage Report</title>
<style>
 body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem auto; max-width: 860px; color: #222; }}
 h1 {{ font-size: 1.5rem; }} h2 {{ font-size: 1.15rem; margin-top: 2rem; }}
 table {{ border-collapse: collapse; width: 100%; margin-top: .5rem; }}
 th, td {{ text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #e3e3e3; }}
 th {{ background: #f6f7f9; }} .num {{ text-align: right; }}
 .card {{ background: #f6f7f9; border-radius: 8px; padding: 1rem 1.2rem; margin: .5rem 0; }}
 .muted {{ color: #666; font-size: .85rem; }}
</style></head><body>
<h1>Chat·hache — Usage Report</h1>
<p class="muted">Generated {agg['generated_at']} · first usage {agg['first_usage_at'] or '—'} · last {agg['last_usage_at'] or '—'}</p>
<div class="card">
 <b>{total['calls']}</b> calls · <b>{fmt_tokens(total['total_tokens'])}</b> tokens
 (<b>{fmt_tokens(total['input_tokens'])}</b> in / <b>{fmt_tokens(total['output_tokens'])}</b> out)
 · est. cost <b>${total['cost_usd']:.4f}</b>
</div>
<h2>Usage — last 30 days</h2>
{sparkline_svg(series)}
<h2>Per user</h2>
<table><tr><th>User</th><th>Calls</th><th class="num">In</th><th class="num">Out</th><th class="num">Total</th><th class="num">Est. cost</th></tr>{rows_user}</table>
<h2>Per model</h2>
<table><tr><th>Model</th><th>Calls</th><th class="num">In</th><th class="num">Out</th><th class="num">Total</th><th class="num">Est. cost</th></tr>{rows_model}</table>
<p class="muted">Costs are estimates from the price table in scripts/telemetry_report.py (per-1M-token list prices); actual billing may differ.</p>
</body></html>"""


def main() -> int:
    rows = collect()
    agg = aggregate(rows)
    (TELEMETRY_DIR / "report.json").write_text(json.dumps(agg, indent=2), encoding="utf-8")
    (TELEMETRY_DIR / "report.html").write_text(render_html(agg), encoding="utf-8")
    t = agg["total"]
    print(
        f"OK: {t['calls']} calls, {t['total_tokens']} tokens "
        f"({t['input_tokens']} in / {t['output_tokens']} out), "
        f"est ${t['cost_usd']:.4f} — report.html + report.json written"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
