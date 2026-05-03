#!/usr/bin/env python3
"""Generate a GitHub-style contribution calendar heatmap SVG from git log."""

import subprocess
import datetime

# ── Config ──────────────────────────────────────────────────────────────────
CELL = 11          # cell size in px
GAP = 2            # gap between cells
CORNER = 2         # border radius
LEFT_PAD = 32      # space for month labels
TOP_PAD = 16       # space for day labels
COLORS = ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"]
OUT_FILE = "img/contribution-calendar.svg"
# ────────────────────────────────────────────────────────────────────────────

def get_commit_map():
    """Return {date_str: count} for the last 365 days."""
    since = (datetime.date.today() - datetime.timedelta(days=364)).isoformat()
    result = subprocess.run(
        ["git", "log", "--format=%ad", "--date=format:%Y-%m-%d", f"--since={since}"],
        capture_output=True, text=True
    )
    counts = {}
    for line in result.stdout.strip().splitlines():
        if line:
            counts[line] = counts.get(line, 0) + 1
    return counts

def level(count):
    """Map commit count to color level 0-4."""
    if count <= 0:  return 0
    if count <= 2:  return 1
    if count <= 4:  return 2
    if count <= 6:  return 3
    return 4

def main():
    counts = get_commit_map()
    today = datetime.date.today()
    # Find the Sunday of the current week
    end = today + datetime.timedelta(days=(6 - today.weekday()) % 7)
    start = end - datetime.timedelta(days=364)
    # Adjust to start on Sunday
    start = start - datetime.timedelta(days=start.weekday() + 1) if start.weekday() != 6 else start

    # Build week columns
    weeks = []
    current = start
    while current <= end:
        week = []
        for d in range(7):
            day = current + datetime.timedelta(days=d)
            if day <= end:
                week.append(day)
        if week:
            weeks.append(week)
        current += datetime.timedelta(days=7)

    num_weeks = len(weeks)
    svg_w = LEFT_PAD + num_weeks * (CELL + GAP) + 8
    svg_h = TOP_PAD + 7 * (CELL + GAP) + 8

    # Month labels
    months = []
    last_month = -1
    for wi, week in enumerate(weeks):
        m = week[0].month
        if m != last_month:
            months.append((wi, ["Jan","Feb","Mar","Apr","May","Jun",
                                "Jul","Aug","Sep","Oct","Nov","Dec"][m-1]))
            last_month = m

    day_labels = ["", "Mon", "", "Wed", "", "Fri", ""]

    lines = []
    lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w}" height="{svg_h}" '
                 f'style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;font-size:10px">')
    lines.append(f'<style>text{{fill:#656d76}}</style>')

    # Month labels
    for wi, name in months:
        x = LEFT_PAD + wi * (CELL + GAP)
        lines.append(f'<text x="{x}" y="10">{name}</text>')

    # Day labels
    for di, label in enumerate(day_labels):
        if label:
            y = TOP_PAD + di * (CELL + GAP) + CELL - 1
            lines.append(f'<text x="0" y="{y}">{label}</text>')

    # Cells
    for wi, week in enumerate(weeks):
        for di, day in enumerate(week):
            key = day.isoformat()
            cnt = counts.get(key, 0)
            color = COLORS[level(cnt)]
            x = LEFT_PAD + wi * (CELL + GAP)
            y = TOP_PAD + di * (CELL + GAP)
            tooltip = f"{day.strftime('%b %d, %Y')}: {cnt} commit{'s' if cnt != 1 else ''}"
            lines.append(f'<rect x="{x}" y="{y}" width="{CELL}" height="{CELL}" rx="{CORNER}" ry="{CORNER}" '
                         f'fill="{color}"><title>{tooltip}</title></rect>')

    lines.append("</svg>")

    with open(OUT_FILE, "w") as f:
        f.write("\n".join(lines))
    print(f"Generated {OUT_FILE} ({num_weeks} weeks)")

if __name__ == "__main__":
    main()
