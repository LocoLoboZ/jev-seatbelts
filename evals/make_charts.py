"""Build the two README test charts from real eval data.

Reads evals/results/*.json (saved runs, not tracked) and the git history of
evals/baseline/gate3.json. Writes SVGs into docs/images/.

Run from anywhere: python evals/make_charts.py
The first chart needs the local evals/results runs. A fresh clone has none.
"""
import glob
import json
import os
import subprocess
from xml.sax.saxutils import escape

REPO = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir))
OUT = os.path.join(REPO, "docs", "images")
NAMES = {"gate1": "Gate 1  plan check", "gate2": "Gate 2  package check",
         "gate3": "Gate 3  command safety", "gate4": "Gate 4  commit screening",
         "gate5": "Gate 5  debug triage", "gate6": "Gate 6  code quality",
         "gate7": "Gate 7  completion check",
         "pipeline": "All gates together (pipeline)"}
GREEN, AMBER, INK, MUTED, GRID = "#2e7d4f", "#d08a1c", "#1f2328", "#57606a", "#d0d7de"
FONT = "font-family=\"-apple-system,Segoe UI,Helvetica,Arial,sans-serif\""


def runs():
    agg = {k: [0, 0, 0, 0] for k in NAMES}  # runs, clean, cases, correct
    first = last = None
    for f in sorted(glob.glob(os.path.join(REPO, "evals", "results", "*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        key = os.path.basename(f).split("-")[0]
        a = agg[key]
        a[0] += 1
        if key == "pipeline":
            a[1] += d.get("overall") == "PASS"
        else:
            a[1] += d["n"] == d["correct"]
            a[2] += d["n"]
            a[3] += d["correct"]
        day = d["ts"][:10]
        first = min(first or day, day)
        last = max(last or day, day)
    return agg, first, last


def text(x, y, s, size=13, fill=INK, anchor="start", weight="normal"):
    return (f'<text x="{x}" y="{y}" {FONT} font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{escape(s)}</text>')


def card(w, h, body, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img" aria-label="{escape(title)}">'
            f'<title>{escape(title)}</title>'
            f'<rect x="0.5" y="0.5" width="{w-1}" height="{h-1}" rx="10" '
            f'fill="#ffffff" stroke="{GRID}"/>' + "".join(body) + "</svg>\n")


def chart_runs(agg, first, last):
    w, left, right, top, row = 820, 250, 190, 92, 34
    total_runs = sum(a[0] for a in agg.values())
    clean = sum(a[1] for a in agg.values())
    cases = sum(a[2] for a in agg.values())
    correct = sum(a[3] for a in agg.values())
    maxr = max(a[0] for a in agg.values())
    span = w - left - right
    h = top + row * len(agg) + 64
    b = [text(24, 36, "Every saved test run, per gate", 18, weight="bold"),
         text(24, 60, f"{total_runs} runs from {first} to {last}. "
              f"{cases:,} test-case checks, {correct:,} correct.", 13, MUTED),
         f'<rect x="{left}" y="70" width="12" height="12" fill="{GREEN}"/>',
         text(left + 18, 81, "every case passed", 12, MUTED),
         f'<rect x="{left + 150}" y="70" width="12" height="12" fill="{AMBER}"/>',
         text(left + 168, 81, "some cases failed", 12, MUTED)]
    for i, (k, (n, ok, _c, _k)) in enumerate(agg.items()):
        y = top + i * row
        gw = span * ok / maxr
        aw = span * (n - ok) / maxr
        b.append(text(24, y + 20, NAMES[k], 13))
        b.append(f'<rect x="{left}" y="{y + 6}" width="{gw:.1f}" height="20" '
                 f'rx="3" fill="{GREEN}"/>')
        if aw:
            b.append(f'<rect x="{left + gw:.1f}" y="{y + 6}" width="{aw:.1f}" '
                     f'height="20" rx="3" fill="{AMBER}"/>')
        label = f"{n} runs" + (f", {n - ok} with a failure" if n - ok else ", all clean")
        b.append(text(left + gw + aw + 8, y + 21, label, 12, MUTED))
    b.append(text(24, h - 22, f"{clean} of {total_runs} runs passed every case. "
                  "Every gate's latest run passes all of its cases.", 12, MUTED))
    return card(w, h, b, "Every saved test run, per gate")


def gate3_growth():
    hashes = subprocess.run(
        ["git", "log", "--reverse", "--format=%H %ad", "--date=short", "--",
         "evals/baseline/gate3.json"], cwd=REPO, capture_output=True,
        text=True, check=True).stdout.split("\n")
    pts = []
    for line in filter(None, hashes):
        h, day = line.split()
        d = json.loads(subprocess.run(
            ["git", "show", f"{h}:evals/baseline/gate3.json"], cwd=REPO,
            capture_output=True, text=True, check=True).stdout)
        if not pts or pts[-1][1] != d["n"]:
            pts.append((day, d["n"], d["correct"]))
    return pts


def chart_growth(pts):
    w, h, left, right, top, bottom = 820, 330, 70, 40, 84, 60
    lo, hi = 0, 140
    span_x = w - left - right
    span_y = h - top - bottom
    pad = 40
    xs = [left + pad + (span_x - 2 * pad) * i / (len(pts) - 1)
          for i in range(len(pts))]
    ys = [top + span_y * (1 - (n - lo) / (hi - lo)) for _d, n, _c in pts]
    b = [text(24, 36, "Gate 3 test cases over time", 18,
              weight="bold"),
         text(24, 60, f"{pts[0][1]} cases on {pts[0][0]} to {pts[-1][1]} on "
              f"{pts[-1][0]}. Every stored baseline passed all of its cases.",
              13, MUTED)]
    for v in range(0, hi + 1, 20):
        y = top + span_y * (1 - v / hi)
        b.append(f'<line x1="{left}" y1="{y:.1f}" x2="{w - right}" '
                 f'y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        b.append(text(left - 10, y + 4, str(v), 11, MUTED, "end"))
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}"
                    for i, (x, y) in enumerate(zip(xs, ys)))
    b.append(f'<path d="{path}" fill="none" stroke="{GREEN}" '
             f'stroke-width="3"/>')
    for (day, n, c), x, y in zip(pts, xs, ys):
        b.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{GREEN}"/>')
        b.append(text(x, y - 12, f"{c}/{n}", 12, INK, "middle", "bold"))
        b.append(text(x, h - bottom + 22, day[5:], 11, MUTED, "middle"))
    b.append(text(left, h - 14, "Date (2026, month-day) of each stored "
                  "baseline where the case count changed", 11, MUTED))
    return card(w, h, b, "Gate 3 test cases over time")


if __name__ == "__main__":
    agg, first, last = runs()
    if not first:
        raise SystemExit("no saved runs in evals/results - run the evals first")
    with open(os.path.join(OUT, "test-runs-per-gate.svg"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(chart_runs(agg, first, last))
    pts = gate3_growth()
    with open(os.path.join(OUT, "gate3-test-growth.svg"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(chart_growth(pts))
    print(agg, pts)
