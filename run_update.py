"""Scheduled job: fetch KEMP obhistory, append to a running CSV, refresh the 14-day chart.

Designed to be safe to run repeatedly (e.g. every 3 hours) — rows are deduplicated
by (date, time). Writes a log line per run to update.log.
"""

from __future__ import annotations

import csv
import os
import re
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import seaborn as sns

STATION = "KEMP"
URL = f"https://forecast.weather.gov/data/obhistory/{STATION}.html"
USER_AGENT = "kemp-obhistory-scraper (contact: rjorintas@gmail.com)"
CHART_DAYS = 14

HERE = Path(__file__).resolve().parent
RUNNING_CSV = HERE / f"{STATION}_obhistory_running.csv"
RAIN_CSV = HERE / f"{STATION}_rainfall_running.csv"
CHART_PNG = HERE / f"{STATION}_rainfall_14d.png"
LOG = HERE / "update.log"

COLUMNS = [
    "Date (day)", "Time (cdt)", "Wind (mph)", "Vis (mi)", "Weather",
    "Sky Cond", "Air Temp (F)", "Dew Pt (F)", "6hr Max (F)", "6hr Min (F)",
    "Relative Humidity", "Wind Chill (F)", "Heat Index (F)",
    "Altimeter (inHg)", "Sea Level (mb)",
    "Precip 1hr (in)", "Precip 3hr (in)", "Precip 6hr (in)",
]
FULL_COLS = ["Date (full)"] + COLUMNS
RAIN_COLS = ["Date (full)", "Date (day)", "Time (cdt)", "Weather",
             "Precip 1hr (in)", "Precip 3hr (in)", "Precip 6hr (in)"]


def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n"
    print(line, end="", file=sys.stderr)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line)


def fetch_html() -> str:
    req = Request(URL, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_rows(html: str) -> list[dict]:
    tbody = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    if not tbody:
        raise RuntimeError("Could not find <tbody> in obhistory page")
    rows: list[dict] = []
    for tr_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", tbody.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr_match.group(1), re.S)
        if len(cells) != len(COLUMNS):
            continue
        clean = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
        rows.append(dict(zip(COLUMNS, clean)))
    return rows


def add_absolute_date(rows: list[dict]) -> None:
    """Page shows day-of-month only. Rows are newest-first; walk down and
    decrement the month when the day-number increases (we crossed a boundary)."""
    today = date.today()
    cur_year, cur_month = today.year, today.month
    prev_day = None
    for row in rows:
        try:
            day = int(row["Date (day)"])
        except (ValueError, KeyError):
            row["Date (full)"] = ""
            continue
        if prev_day is None and day > today.day:
            cur_month -= 1
            if cur_month == 0:
                cur_month, cur_year = 12, cur_year - 1
        elif prev_day is not None and day > prev_day:
            cur_month -= 1
            if cur_month == 0:
                cur_month, cur_year = 12, cur_year - 1
        try:
            row["Date (full)"] = date(cur_year, cur_month, day).isoformat()
        except ValueError:
            row["Date (full)"] = ""
        prev_day = day


def load_running(path: Path) -> dict[tuple[str, str], dict]:
    if not path.exists():
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            key = (r.get("Date (full)", ""), r.get("Time (cdt)", ""))
            if key[0] and key[1]:
                out[key] = r
    return out


def write_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in cols})


def to_float(s: str) -> float:
    s = (s or "").strip()
    if not s or s == "-":
        return 0.0
    if s.lower() in ("t", "trace"):
        return 0.005
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_ts(row: dict) -> datetime | None:
    d = row.get("Date (full)") or ""
    t = row.get("Time (cdt)") or ""
    if not d or not t:
        return None
    try:
        return datetime.fromisoformat(f"{d}T{t}")
    except ValueError:
        return None


def render_chart(all_rows: list[dict]) -> int:
    """Render daily rainfall totals for the last CHART_DAYS days.

    Sums the 1-hour precipitation values from each standard hourly observation
    (METAR reports at minute :53) into daily buckets in local CDT. Each :53 row
    reports rain that fell in the prior 60 minutes, so non-:53 SPECIs are skipped
    to avoid double-counting overlapping windows.
    """
    today_local = date.today()
    cutoff_date = today_local - timedelta(days=CHART_DAYS - 1)
    totals: dict[date, float] = {
        cutoff_date + timedelta(days=i): 0.0 for i in range(CHART_DAYS)
    }

    rows_used = 0
    for r in all_rows:
        ts = parse_ts(r)
        if ts is None:
            continue
        d = ts.date()
        if d < cutoff_date or d > today_local:
            continue
        if ts.minute < 50:
            continue  # skip SPECIs to avoid overlapping the :53 hourly METAR
        totals[d] = totals.get(d, 0.0) + to_float(r.get("Precip 1hr (in)", ""))
        rows_used += 1

    days = sorted(totals.keys())
    values = [totals[d] for d in days]

    sns.set_theme(style="whitegrid", context="talk", font_scale=0.75)
    palette = sns.color_palette("crest", as_cmap=False, n_colors=6)
    bar_color = palette[3]
    dry_color = "#e6ecf0"

    fig, ax = plt.subplots(figsize=(13, 5))
    colors = [bar_color if v > 0 else dry_color for v in values]
    ax.bar(days, values, width=0.8, color=colors,
           edgecolor="white", linewidth=0.6)

    for d, v in zip(days, values):
        if v > 0:
            ax.annotate(f"{v:.2f}\"", xy=(d, v),
                        xytext=(0, 4), textcoords="offset points",
                        ha="center", fontsize=9, color=bar_color,
                        weight="bold")

    ax.set_title(f"KEMP — Emporia, KS  ·  Daily rainfall, last {CHART_DAYS} days",
                 fontsize=14, weight="bold", loc="left")
    fig.text(0.01, 0.94,
             f"Daily totals in inches  ·  Source: forecast.weather.gov/data/obhistory/{STATION}.html"
             f"  ·  Updated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             fontsize=8, color="#666")
    ax.set_xlabel("")
    ax.set_ylabel("Rainfall (in)")
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a\n%b %d"))
    ax.set_xlim(days[0] - timedelta(days=0.5), days[-1] + timedelta(days=0.5))
    ymax = max(values) if values else 0.0
    ax.set_ylim(0, max(0.1, ymax * 1.25))
    sns.despine(ax=ax, left=True)
    ax.tick_params(axis="y", left=False)
    ax.grid(True, axis="y", linestyle="-", alpha=0.35)
    ax.grid(False, axis="x")

    total = sum(values)
    wet_days = sum(1 for v in values if v > 0)
    ax.text(0.99, 0.95,
            f"{CHART_DAYS}-day total:  {total:.2f}\"\n"
            f"Days with rain:  {wet_days}",
            transform=ax.transAxes, ha="right", va="top", fontsize=10,
            bbox=dict(facecolor="white", edgecolor="#ddd",
                      boxstyle="round,pad=0.5"))

    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(CHART_PNG, dpi=140, facecolor="white")
    plt.close(fig)
    return rows_used


def main() -> int:
    try:
        log("run start")
        html = fetch_html()
        new_rows = parse_rows(html)
        add_absolute_date(new_rows)
        log(f"scraped {len(new_rows)} rows from page")

        existing = load_running(RUNNING_CSV)
        added = 0
        updated = 0
        for r in new_rows:
            key = (r.get("Date (full)", ""), r.get("Time (cdt)", ""))
            if not key[0] or not key[1]:
                continue
            if key in existing:
                if existing[key] != {c: r.get(c, "") for c in FULL_COLS}:
                    updated += 1
            else:
                added += 1
            existing[key] = {c: r.get(c, "") for c in FULL_COLS}

        merged = sorted(
            existing.values(),
            key=lambda r: (r.get("Date (full)", ""), r.get("Time (cdt)", "")),
            reverse=True,
        )
        write_csv(RUNNING_CSV, merged, FULL_COLS)
        rain_only = [r for r in merged if any(
            r.get(c) for c in ("Precip 1hr (in)", "Precip 3hr (in)", "Precip 6hr (in)")
        )]
        write_csv(RAIN_CSV, rain_only, RAIN_COLS)
        log(f"merged: total={len(merged)}, new={added}, updated={updated}, "
            f"with-precip={len(rain_only)}")

        plotted = render_chart(merged)
        log(f"chart rendered with {plotted} points -> {CHART_PNG.name}")
        log("run ok")
        return 0
    except Exception as exc:
        log(f"run FAILED: {exc.__class__.__name__}: {exc}")
        with open(LOG, "a", encoding="utf-8") as fh:
            traceback.print_exc(file=fh)
        return 1


if __name__ == "__main__":
    sys.exit(main())
