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
    cutoff = datetime.now() - timedelta(days=CHART_DAYS)
    pts = []
    for r in all_rows:
        ts = parse_ts(r)
        if ts is None or ts < cutoff:
            continue
        pts.append((ts,
                    to_float(r.get("Precip 1hr (in)", "")),
                    to_float(r.get("Precip 3hr (in)", "")),
                    to_float(r.get("Precip 6hr (in)", ""))))
    pts.sort(key=lambda x: x[0])
    if not pts:
        log("no data points in the 14-day window; skipping chart")
        return 0
    times, p1, p3, p6 = zip(*pts)

    sns.set_theme(style="whitegrid", context="talk", font_scale=0.7)
    palette = sns.color_palette("crest", 3)
    fig, ax = plt.subplots(figsize=(14, 5.5))

    span_hours = (max(times) - min(times)).total_seconds() / 3600 or 1
    width = max(0.005, min(0.05, 1 / max(24, span_hours / 4)))

    # Plot 6hr/3hr in back as wider, lighter bars; 1hr on top sharp & opaque.
    ax.bar(times, p6, width=width * 4, color=palette[0], alpha=0.30,
           label="6 hr", align="center", edgecolor="none")
    ax.bar(times, p3, width=width * 2.2, color=palette[1], alpha=0.55,
           label="3 hr", align="center", edgecolor="none")
    ax.bar(times, p1, width=width, color=palette[2], alpha=1.0,
           label="1 hr", align="center", edgecolor="white", linewidth=0.4)

    ax.set_title(f"KEMP — Emporia, KS  |  Rainfall, last {CHART_DAYS} days",
                 fontsize=14, weight="bold", loc="left")
    fig.text(0.01, 0.93,
             f"Source: forecast.weather.gov/data/obhistory/{STATION}.html  "
             f"· Updated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             fontsize=8, color="#666")
    ax.set_xlabel("")
    ax.set_ylabel("Precipitation (in)")
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[0, 6, 12, 18]))
    ax.grid(True, axis="y", linestyle="-", alpha=0.35)
    ax.grid(True, axis="x", which="major", linestyle="-", alpha=0.25)
    sns.despine(ax=ax, left=True)
    ax.tick_params(axis="y", left=False)
    ax.legend(loc="upper left", frameon=False, ncol=3)

    for ts, v in zip(times, p1):
        if v > 0:
            ax.annotate(f"{v:.2f}", xy=(ts, v),
                        xytext=(0, 4), textcoords="offset points",
                        ha="center", fontsize=7, color=palette[2])

    total_1hr = sum(p1)
    nonzero = sum(1 for v in p1 if v > 0)
    ax.text(0.99, 0.95,
            f"Σ 1-hr values: {total_1hr:.2f} in\n"
            f"Hours w/ precip: {nonzero}",
            transform=ax.transAxes, ha="right", va="top", fontsize=9,
            bbox=dict(facecolor="white", edgecolor="#ddd", boxstyle="round,pad=0.4"))

    fig.autofmt_xdate()
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(CHART_PNG, dpi=140, facecolor="white")
    plt.close(fig)
    return len(pts)


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
