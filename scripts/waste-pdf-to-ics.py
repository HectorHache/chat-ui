#!/usr/bin/env python3
"""waste-pdf-to-ics.py — RMN (or similar) Afvalkalender PDF → iCal.

Parses the municipality waste calendar PDF (RMN Nieuwegein format: a
1-page A4 with 12 monthly grids, 3 months per band, colored collection
swatches below each day number) and emits a standard .ics file that the
household_waste tool can read.

Usage:
  env/bin/python scripts/waste-pdf-to-ics.py --pdf afval2026.pdf --year 2026 \
      --out data/waste/afval2026.ics

Markers are tiny text chars ('1'/'t') whose FILL COLOR identifies the
waste type (verified against the PDF legend + vision cross-check):
  grey        -> Restafval
  dark green  -> GFT
  blue        -> Papier & Karton
  light green -> Kerstboom
Day numbers are positioned per cell; rows run Saturday→Friday
(i.e. the grid starts on the Monday before the 1st, or on the 1st itself
when it IS a Monday). Holiday shifts are already baked into the PDF.

Run again with next year's PDF when RMN publishes it (December).
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta

try:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTChar, LTTextContainer, LTTextLine
except ImportError:
    sys.exit("pdfminer.six is required: uv pip install --python env/bin/python pdfminer.six")

MONTHS = ["Januari", "Februari", "Maart", "April", "Mei", "Juni",
          "Juli", "Augustus", "September", "Oktober", "November", "December"]
MONTH_N = {m: i + 1 for i, m in enumerate(MONTHS)}
COLOR_TYPE = {
    (0.7529,): "Restafval",
    (0.0824, 0.4549, 0.0471): "GFT",
    (0.2431, 0.3529, 0.9804): "Papier & Karton",
    (0.8039, 1.0, 0.5804): "Kerstboom",
}
# English-only output (B22): type names as shown to users
EN = {"Restafval": "Residual waste", "GFT": "Organic waste (GFT)",
      "Papier & Karton": "Paper & cardboard", "Kerstboom": "Christmas trees"}


def colkey(c):
    return tuple(round(x, 4) for x in c) if isinstance(c, tuple) else (round(c, 4),)


def extract_chars(pdf_path):
    chars = []
    for page in extract_pages(pdf_path):
        def walk(objs):
            for o in objs:
                if isinstance(o, LTTextContainer):
                    for line in o:
                        if isinstance(line, LTTextLine):
                            for ch in line:
                                if isinstance(ch, LTChar):
                                    try:
                                        c = ch.graphicstate.ncolor
                                    except Exception:
                                        c = None
                                    chars.append({"y": ch.y0, "x": ch.x0,
                                                  "t": ch.get_text(), "s": ch.size, "c": c})
                elif hasattr(o, '__iter__'):
                    walk(o)
        walk(page._objs if hasattr(page, '_objs') else [])
    return chars


def parse(pdf_path, year):
    chars = extract_chars(pdf_path)

    # month labels (bold 10pt words, widely tracked)
    labels = []
    ys = sorted(set(round(c["y"], 1) for c in chars), reverse=True)
    for y in ys:
        line = sorted([c for c in chars if abs(c["y"] - y) < 1.0 and 9.0 < c["s"] < 11.0],
                      key=lambda c: c["x"])
        word, lastx, startx = "", None, None
        for c in line:
            if lastx is not None and c["x"] - lastx > 9.5:
                if word in MONTHS:
                    labels.append((y, word, startx))
                word, startx = "", None
            if word == "":
                startx = c["x"]
            word += c["t"]
            lastx = c["x"]
        if word in MONTHS:
            labels.append((y, word, startx))
    labels = sorted(set(labels), key=lambda p: -p[0])
    if len(labels) != 12:
        raise SystemExit(f"expected 12 month labels, found {len(labels)}: {labels}")

    COLS = [(0, 195), (195, 360), (360, 700)]
    band_ys = sorted(set(y for y, _, _ in labels), reverse=True)

    def yrange(y_label):
        below = [b for b in band_ys if b < y_label - 10]
        return (max(below) if below else 101.0), y_label

    events, problems = set(), []
    for y_label, mname, lx in labels:
        mn = MONTH_N[mname]
        lo, hi = yrange(y_label)
        col = next((i for i, (a, b) in enumerate(COLS) if a <= lx < b), None)
        if col is None:
            problems.append(f"{mname}: no column"); continue
        xa, xb = COLS[col]
        day_chars = [c for c in chars if lo < c["y"] <= hi and xa <= c["x"] < xb
                     and 4.5 < c["s"] < 6.5 and c["t"].isdigit()]
        mk_chars = [c for c in chars if lo < c["y"] <= hi and xa <= c["x"] < xb
                    and c["s"] < 2.0 and c["t"] == "t"]
        day_chars.sort(key=lambda c: (-round(c["y"], 1), c["x"]))

        rows = defaultdict(list)
        for c in day_chars:
            k = next((rk for rk in rows if abs(rk - c["y"]) < 2.0), None)
            rows[k if k is not None else c["y"]].append(c)

        cells = []
        for rk in sorted(rows, reverse=True):
            tok = []
            for c in sorted(rows[rk], key=lambda c: c["x"]):
                if tok and c["x"] - tok[-1]["x"] < 4.0:
                    tok.append(c)
                else:
                    if tok:
                        cells.append((rk, min(t["x"] for t in tok),
                                      int("".join(t["t"] for t in sorted(tok, key=lambda t: t["x"])))))
                    tok = [c]
            if tok:
                cells.append((rk, min(t["x"] for t in tok),
                              int("".join(t["t"] for t in sorted(tok, key=lambda t: t["x"])))))

        d0 = cells[0][2]
        if d0 <= 7:
            cur = date(year, mn, 1) if d0 == 1 else date(year, mn, 1) - timedelta(days=d0 - 1)
        else:
            prev_last = date(year - 1, 12, 31) if mn == 1 else (date(year, mn, 1) - timedelta(days=1))
            cur = prev_last - timedelta(days=prev_last.day - d0)

        day_dates = {}
        for (rk, x, n) in cells:
            if n != cur.day:
                problems.append(f"{mname}: cell {n} exp {cur.day}")
            day_dates[(round(rk, 0), round(x, 0))] = cur
            cur += timedelta(days=1)

        for m in mk_chars:
            best, bd = None, 1e9
            for (dy, dx), dt in day_dates.items():
                if abs(dx - m["x"]) < 4.0:
                    dd = abs(dy - m["y"])
                    if dd < bd:
                        bd, best = dd, dt
            if best is None:
                problems.append(f"{mname}: orphan marker {m['x']:.0f},{m['y']:.0f}"); continue
            typ = COLOR_TYPE.get(colkey(m["c"]))
            if typ is None:
                problems.append(f"{mname}: unknown color {m['c']} at {best}"); continue
            events.add((best, typ))

    return sorted(events), problems


def to_ics(events, cal_name):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//Chat-hache//Afvalkalender//NL",
             "CALSCALE:GREGORIAN", f"X-WR-CALNAME:{cal_name}"]
    for (d, t) in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:waste-{d.isoformat()}-{t.replace(' ','').lower()}@chat.hector.app",
            f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
            f"SUMMARY:{EN[t]}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main():
    ap = argparse.ArgumentParser(description="RMN Afvalkalender PDF -> ICS")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--json", help="also write events as JSON to this path")
    args = ap.parse_args()

    events, problems = parse(args.pdf, args.year)
    if problems:
        print("WARN:", len(problems), "problems")
        for p in problems[:10]:
            print("  ", p)

    counts = Counter(t for _, t in events)
    print(f"events: {len(events)}  types: {dict(counts)}")
    if not (24 <= counts.get("Restafval", 0) <= 27) or not (20 <= counts.get("GFT", 0) <= 26) \
            or not (10 <= counts.get("Papier & Karton", 0) <= 15) or counts.get("Kerstboom", 0) > 5:
        print("WARN: type counts look unusual — inspect before trusting")

    with open(args.out, "w") as f:
        f.write(to_ics(events, f"Afvalkalender {args.year}"))
    print("wrote", args.out)
    if args.json:
        with open(args.json, "w") as f:
            json.dump([[d.isoformat(), t] for d, t in events], f, indent=0)
        print("wrote", args.json)


if __name__ == "__main__":
    main()
