#!/usr/bin/env python3
"""Generate a GitHub-style contribution calendar heatmap SVG from git log.
Supports light/dark mode via prefers-color-scheme, and only shows dates
from the project's first commit onwards."""

import subprocess
import datetime

# ── Config ──────────────────────────────────────────────────────────────────
CELL = 13
GAP = 3
CORNER = 2
LEFT_PAD = 38
TOP_PAD = 22

# Light mode colors
LIGHT_EMPTY = "#ebedf0"
LIGHT = ["#9be9a8", "#40c463", "#30a14e", "#216e39"]
LIGHT_TEXT = "#656d76"

# Dark mode colors
DARK_EMPTY = "#2d333b"
DARK = ["#0e4429", "#006d32", "#26a641", "#39d353"]
DARK_TEXT = "#8b949e"

OUT_FILE = "img/contribution-calendar.svg"
# ────────────────────────────────────────────────────────────────────────────

def get_commit_map():
    """Return {date_str: count} for all commits."""
    result = subprocess.run(
        ["git", "log", "--format=%ad", "--date=format:%Y-%m-%d"],
        capture_output=True, text=True
    )
    counts = {}
    for line in result.stdout.strip().splitlines():
        if line:
            counts[line] = counts.get(line, 0) + 1
    return counts

def get_first_commit_date():
    """Return the date of the earliest commit."""
    result = subprocess.run(
        ["git", "log", "--reverse", "--format=%ad", "--date=format:%Y-%m-%d"],
        capture_output=True, text=True
    )
    first = result.stdout.strip().splitlines()[0]
    return datetime.date.fromisoformat(first)

def level(count):
    if count <= 0:  return -1
    if count <= 2:  return 0
    if count <= 4:  return 1
    if count <= 6:  return 2
    return 3

def main():
    counts = get_commit_map()
    first_date = get_first_commit_date()
    today = datetime.date.today()

    # Start from Sunday of the first commit's week
    start = first_date - datetime.timedelta(days=(first_date.weekday() + 1) % 7)
    # End on Saturday of the current week
    end = today + datetime.timedelta(days=(6 - today.weekday()) % 7)

    weeks = []
    current = start
    while current <= end:
        week = []
        for d in range(7):
            day = current + datetime.timedelta(days=d)
            week.append(day if first_date <= day <= end else None)
        weeks.append(week)
        current += datetime.timedelta(days=7)

    num_weeks = len(weeks)
    svg_w = LEFT_PAD + num_weeks * (CELL + GAP) + 8
    svg_h = TOP_PAD + 7 * (CELL + GAP) + 10

    # Month labels
    months = []
    last_month = -1
    for wi, week in enumerate(weeks):
        for day in week:
            if day and day >= first_date:
                m = day.month
                if m != last_month:
                    months.append((wi, ["Jan","Feb","Mar","Apr","May","Jun",
                                        "Jul","Aug","Sep","Oct","Nov","Dec"][m-1]))
                    last_month = m
                break

    day_labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="' + str(svg_w) + '" height="' + str(svg_h) + '">')

    # CSS with dark/light mode
    out.append('<style>')
    out.append("  text { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 11px }")
    out.append('  .label { fill: ' + LIGHT_TEXT + ' }')
    out.append('  .empty { fill: ' + LIGHT_EMPTY + ' }')
    for i in range(4):
        out.append('  .l' + str(i) + ' { fill: ' + LIGHT[i] + ' }')
    out.append('  @media (prefers-color-scheme: dark) {')
    out.append('    .label { fill: ' + DARK_TEXT + ' }')
    out.append('    .empty { fill: ' + DARK_EMPTY + ' }')
    for i in range(4):
        out.append('    .l' + str(i) + ' { fill: ' + DARK[i] + ' }')
    out.append('  }')
    out.append('</style>')

    # Month labels
    for wi, name in months:
        x = LEFT_PAD + wi * (CELL + GAP)
        out.append('<text class="label" x="' + str(x) + '" y="14">' + name + '</text>')

    # Day labels (Mon, Wed, Fri)
    for di, label in enumerate(day_labels):
        if di % 2 == 1:
            y = TOP_PAD + di * (CELL + GAP) + CELL - 2
            out.append('<text class="label" x="0" y="' + str(y) + '">' + label + '</text>')

    # Cells
    for wi, week in enumerate(weeks):
        for di, day in enumerate(week):
            if day is None or day < first_date:
                continue
            x = LEFT_PAD + wi * (CELL + GAP)
            y = TOP_PAD + di * (CELL + GAP)
            cnt = counts.get(day.isoformat(), 0)
            lvl = level(cnt)
            cls = "empty" if lvl < 0 else "l" + str(lvl)
            s = "s" if cnt != 1 else ""
            tip = day.strftime("%b %d, %Y") + ": " + str(cnt) + " commit" + s
            out.append('<rect class="' + cls + '" x="' + str(x) + '" y="' + str(y) + '" '
                       'width="' + str(CELL) + '" height="' + str(CELL) + '" '
                       'rx="' + str(CORNER) + '" ry="' + str(CORNER) + '">'
                       '<title>' + tip + '</title></rect>')

    out.append('</svg>')

    with open(OUT_FILE, "w") as f:
        f.write("\n".join(out))
    print("Generated " + OUT_FILE + " (" + str(num_weeks) + " weeks, from " + str(first_date) + " to " + str(today) + ")")

if __name__ == "__main__":
    main()
