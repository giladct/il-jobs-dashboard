#!/usr/bin/env python3
"""
Israel Dev Jobs Tracker — devjobs.co.il edition
Scrapes devjobs.co.il (Israeli tech-focused job board, ~3,170 listings) for
all open positions.  Aggregates by company name and stores daily snapshots to
SQLite; exports data.js for the Chart.js dashboard.

Free — no API key required.
Requires: pip install requests beautifulsoup4

Usage:
  python jobs_tracker.py                   # collect one snapshot now + export data.js
  python jobs_tracker.py export            # re-export data.js from existing DB
  python jobs_tracker.py loop             # collect daily in a blocking loop
  python jobs_tracker.py status           # print DB summary
  python jobs_tracker.py company <Name>   # scrape + update one company only (fast test)

Notes:
  - Paginates up to MAX_PAGES pages (30 jobs/page) through devjobs.co.il.
  - robots.txt is fully permissive; a polite 1-2 s delay is added between pages.
  - Run once a day (or via Windows Task Scheduler) to build trend history.
"""

import json
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, date
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("ERROR: run:  pip install requests beautifulsoup4")

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH    = Path(__file__).parent / "jobs.db"
DATA_PATH  = Path(__file__).parent / "data.js"

BASE_URL   = "https://devjobs.co.il/jobs-grid"
MAX_PAGES  = 110   # ~106 real pages; stops early when page returns no cards
TOP_N      = 30    # top companies to include in data.js
POLL_HOURS = 24    # interval for loop mode

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://devjobs.co.il/",
}

# ── Developer-type classification ─────────────────────────────────────────────

DEV_TYPE_MAP = [
    ("Full Stack", ["full stack", "fullstack", "full-stack"]),
    ("Frontend",   ["frontend", "front end", "front-end", "react ", "vue ", "angular ",
                    "ui developer", "ui/ux"]),
    ("Backend",    ["backend", "back end", "back-end", "java ", "python ", "node.js",
                    "django", "spring", ".net ", "golang", "ruby", "php "]),
    ("DevOps",     ["devops", "dev ops", "sre", "infrastructure", "cloud engineer",
                    "platform engineer", "kubernetes", "docker"]),
    ("Data/ML",    ["data scientist", "data engineer", "machine learning", "ml engineer",
                    "data analyst", "ai ", "deep learning", "nlp", "llm"]),
    ("Mobile",     ["mobile", " ios ", "android", "react native", "flutter", "swift", "kotlin"]),
    ("QA",         ["qa ", "quality assurance", "automation engineer", "sdet", "tester"]),
    ("Security",   ["security", "cybersecurity", "pentest", "soc ", "appsec"]),
    ("Manager",    ["manager", "team lead", "vp ", "director", "cto", "head of", "r&d lead"]),
    ("Embedded",   ["embedded", "firmware", "fpga", "vhdl", "verilog", "rtos"]),
]

def classify_dev_type(title: str) -> str:
    """Return a developer-type label derived from keyword matching on the job title."""
    t = title.lower()
    for label, keywords in DEV_TYPE_MAP:
        if any(kw in t for kw in keywords):
            return label
    return "Other"

# ── Database ──────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id        INTEGER PRIMARY KEY,
            snap_date TEXT    NOT NULL,
            company   TEXT    NOT NULL,
            cnt       INTEGER NOT NULL,
            UNIQUE(snap_date, company)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS job_records (
            id        INTEGER PRIMARY KEY,
            snap_date TEXT NOT NULL,
            company   TEXT NOT NULL,
            title     TEXT NOT NULL,
            url       TEXT,
            dev_type  TEXT DEFAULT '',
            work_mode TEXT DEFAULT '',
            location  TEXT DEFAULT '',
            job_id    TEXT DEFAULT '',
            UNIQUE(snap_date, company, title)
        )
    """)
    # Migrate older DBs: add columns if they don't exist yet
    for col, dflt in [("dev_type", "''"), ("work_mode", "''"), ("location", "''"), ("job_id", "''")]:
        try:
            con.execute(f"ALTER TABLE job_records ADD COLUMN {col} TEXT DEFAULT {dflt}")
        except Exception:
            pass  # column already exists

    # One-time backfill: extract job_id from url for rows that predate the job_id column
    _ID_RE = re.compile(r'/job-details/(\d+)')
    rows_to_fix = con.execute(
        "SELECT id, url FROM job_records WHERE job_id = '' AND url LIKE '%/job-details/%'"
    ).fetchall()
    if rows_to_fix:
        updates = [(m.group(1), rid) for rid, url in rows_to_fix
                   if (m := _ID_RE.search(url or ""))]
        if updates:
            con.executemany("UPDATE job_records SET job_id = ? WHERE id = ?", updates)
            print(f"  Migrated job_id for {len(updates)} existing job_records rows.")

    # job_index: one canonical row per unique job
    con.execute("""
        CREATE TABLE IF NOT EXISTS job_index (
            job_id       TEXT PRIMARY KEY,
            company      TEXT NOT NULL,
            title        TEXT NOT NULL,
            url          TEXT DEFAULT '',
            dev_type     TEXT DEFAULT '',
            work_mode    TEXT DEFAULT '',
            location     TEXT DEFAULT '',
            first_seen   TEXT NOT NULL,
            last_seen    TEXT NOT NULL,
            date_removed TEXT DEFAULT ''
        )
    """)

    # Bootstrap job_index from job_records history (runs once when job_index is empty)
    ji_count = con.execute("SELECT COUNT(*) FROM job_index").fetchone()[0]
    jr_count  = con.execute("SELECT COUNT(*) FROM job_records WHERE job_id != ''").fetchone()[0]
    if ji_count == 0 and jr_count > 0:
        con.execute("""
            INSERT OR IGNORE INTO job_index
                (job_id, company, title, url, dev_type, work_mode, location,
                 first_seen, last_seen, date_removed)
            SELECT jr.job_id, jr.company, jr.title, jr.url,
                   jr.dev_type, jr.work_mode, jr.location,
                   agg.first_seen, agg.last_seen, ''
            FROM (
                SELECT job_id,
                       MIN(snap_date) AS first_seen,
                       MAX(snap_date) AS last_seen
                FROM job_records WHERE job_id != ''
                GROUP BY job_id
            ) agg
            JOIN job_records jr
              ON jr.job_id = agg.job_id AND jr.snap_date = agg.last_seen
        """)
        bootstrapped = con.execute("SELECT COUNT(*) FROM job_index").fetchone()[0]
        print(f"  Bootstrapped {bootstrapped} jobs into job_index.")

    con.commit()
    return con


def save_snapshot(con: sqlite3.Connection, snap_date: str,
                  company_counts: dict, job_list: list):
    # Aggregated counts (for the chart)
    agg_rows = [(snap_date, co, cnt) for co, cnt in company_counts.items() if cnt > 0]
    con.executemany(
        "INSERT OR REPLACE INTO snapshots(snap_date, company, cnt) VALUES (?,?,?)",
        agg_rows,
    )
    # Individual records (for raw table)
    rec_rows = [
        (snap_date, j["company"], j["title"], j.get("url", ""),
         j.get("dev_type", ""), j.get("work_mode", ""), j.get("location", ""),
         j.get("job_id", ""))
        for j in job_list
    ]
    con.executemany(
        "INSERT OR IGNORE INTO job_records"
        "(snap_date, company, title, url, dev_type, work_mode, location, job_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        rec_rows,
    )
    con.commit()
    print(f"  Saved {len(agg_rows)} companies, {len(rec_rows)} job records -> {DB_PATH.name}")


def update_job_index(con: sqlite3.Connection, job_list: list, today_date: str,
                     mark_removals: bool = True):
    """
    Upsert today's scraped jobs into job_index (one row per unique job).
    If mark_removals=True (full scrape), jobs absent today get date_removed set.
    If mark_removals=False (company-only scrape), only the listed jobs are updated.
    """
    today_ids = {j["job_id"] for j in job_list if j.get("job_id")}

    upsert_rows = [
        (j["job_id"], j["company"], j["title"], j.get("url", ""),
         j.get("dev_type", ""), j.get("work_mode", ""), j.get("location", ""),
         today_date, today_date)
        for j in job_list if j.get("job_id")
    ]
    con.executemany("""
        INSERT INTO job_index
            (job_id, company, title, url, dev_type, work_mode, location,
             first_seen, last_seen, date_removed)
        VALUES (?,?,?,?,?,?,?,?,?,'')
        ON CONFLICT(job_id) DO UPDATE SET
            last_seen    = excluded.last_seen,
            date_removed = '',
            company      = excluded.company,
            title        = excluded.title,
            url          = excluded.url,
            dev_type     = excluded.dev_type,
            work_mode    = CASE WHEN excluded.work_mode != '' THEN excluded.work_mode
                                ELSE job_index.work_mode END,
            location     = CASE WHEN excluded.location != '' THEN excluded.location
                                ELSE job_index.location END
    """, upsert_rows)

    if mark_removals:
        if today_ids:
            # Use a temp table to avoid SQLite's 999-parameter IN-clause limit
            con.execute("CREATE TEMP TABLE IF NOT EXISTS _today_ids (job_id TEXT PRIMARY KEY)")
            con.execute("DELETE FROM _today_ids")
            con.executemany("INSERT INTO _today_ids VALUES (?)", [(jid,) for jid in today_ids])
            con.execute("""
                UPDATE job_index
                SET date_removed = ?
                WHERE date_removed = ''
                  AND job_id NOT IN (SELECT job_id FROM _today_ids)
            """, (today_date,))
            con.execute("DROP TABLE IF EXISTS _today_ids")
        else:
            print("  WARNING: update_job_index called with empty job_list — skipping removal marking.")

    con.commit()
    removed_today = con.execute(
        "SELECT COUNT(*) FROM job_index WHERE date_removed = ?", (today_date,)
    ).fetchone()[0]
    active = con.execute(
        "SELECT COUNT(*) FROM job_index WHERE date_removed = ''"
    ).fetchone()[0]
    print(f"  job_index: {len(upsert_rows)} upserted, {removed_today} removed today, "
          f"{active} active total.")


# ── devjobs.co.il scraper ─────────────────────────────────────────────────────

def fetch_jobs_israel() -> tuple[dict, list]:
    """
    Scrapes devjobs.co.il for all open tech positions in Israel.
    Paginates up to MAX_PAGES pages (30 jobs each).
    Returns ({company: count}, [{company, title, url}, ...]) deduplicated by job ID.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    company_counts: dict[str, int] = {}
    job_list: list[dict] = []
    seen_ids: set[str] = set()
    consecutive_empty = 0

    for page in range(1, MAX_PAGES + 1):
        try:
            resp = session.get(BASE_URL, params={"page": page}, timeout=20)
        except requests.RequestException as exc:
            print(f"  [page {page:3}]: network error - {exc}")
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            continue

        if resp.status_code == 429:
            print(f"  [page {page:3}]: rate-limited, waiting 60 s...")
            time.sleep(60)
            continue
        if resp.status_code != 200:
            print(f"  [page {page:3}]: HTTP {resp.status_code}")
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # Job cards live inside div#jobsGridList
        grid = soup.find("div", id="jobsGridList")
        cards = grid.find_all("div", class_="col-xl-4") if grid else []

        if not cards:
            print(f"  [page {page:3}]: no cards - stopping")
            break

        consecutive_empty = 0
        new = 0

        for card in cards:
            title_el = card.find("a", class_="name-job")
            co_el    = card.find("a", class_="profession")

            title = title_el.get_text(strip=True) if title_el else ""
            co    = co_el.get_text(strip=True)    if co_el    else ""
            href  = title_el.get("href", "")      if title_el else ""

            # Job ID from URL path: /job-details/4417518027
            job_id = href.rstrip("/").split("/")[-1] if href else ""

            if not co or not title or not job_id:
                continue
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            url = href if href.startswith("http") else f"https://devjobs.co.il{href}"

            # ── Location + Work mode (combined in span.location-small) ──
            # Format: "Tel Aviv-Yafo (Hybrid)"  /  "Israel (Remote)"
            loc_el = card.find("span", class_="location-small")
            location_full = loc_el.get_text(strip=True) if loc_el else ""
            m = re.match(r'^(.+?)\s*\(([^)]+)\)$', location_full)
            if m:
                location  = m.group(1).strip()
                work_mode = m.group(2).strip()
            else:
                location  = location_full
                work_mode = ""

            # ── Developer type (derived from job title) ────────────────────
            dev_type = classify_dev_type(title)

            company_counts[co] = company_counts.get(co, 0) + 1
            job_list.append({"company": co, "title": title, "url": url,
                             "dev_type": dev_type, "work_mode": work_mode,
                             "location": location, "job_id": job_id})
            new += 1

        total = sum(company_counts.values())
        print(f"  [page {page:3}]: +{new:2} new  (total: {total})")
        time.sleep(random.uniform(1.0, 2.0))

    print(f"  -> {len(company_counts)} unique companies, {len(job_list)} unique jobs")
    return company_counts, job_list


# ── Export ────────────────────────────────────────────────────────────────────

def export_data_js(con: sqlite3.Connection):
    rows = con.execute(
        "SELECT snap_date, company, cnt FROM snapshots ORDER BY snap_date, cnt DESC"
    ).fetchall()

    if not rows:
        print("No data in DB yet -- run without arguments first to collect a snapshot.")
        return

    # All dates in order
    all_dates = sorted(set(r[0] for r in rows))

    # Pick top-N companies by their highest single-day count
    peak: dict[str, int] = {}
    for _, co, cnt in rows:
        if cnt > peak.get(co, 0):
            peak[co] = cnt
    top_companies = sorted(peak, key=lambda c: peak[c], reverse=True)[:TOP_N]

    # Build datasets array (one per company)
    by_co: dict[str, dict[str, int]] = {}
    for snap_date, co, cnt in rows:
        by_co.setdefault(co, {})[snap_date] = cnt

    datasets = []
    for co in top_companies:
        data_points = [by_co[co].get(d, None) for d in all_dates]
        datasets.append({
            "label": co,
            "data":  data_points,
        })

    # Latest-day totals for summary bar (all companies, not just top N)
    latest_date  = all_dates[-1]
    latest_total = sum(
        by_co[co].get(latest_date, 0) for co in by_co
    )

    # Individual job records: read from job_index (one row per unique job)
    today_str = date.today().isoformat()
    ji_count = con.execute("SELECT COUNT(*) FROM job_index").fetchone()[0]

    if ji_count > 0:
        job_rows = con.execute("""
            SELECT job_id, company, title, url, dev_type, work_mode, location,
                   first_seen, last_seen, date_removed
            FROM job_index
            ORDER BY first_seen DESC, company, title
        """).fetchall()

        # Backfill dev_type for rows that have it empty
        backfill = [(classify_dev_type(r[2]), r[0]) for r in job_rows if not r[4]]
        if backfill:
            con.executemany("UPDATE job_index SET dev_type=? WHERE job_id=?", backfill)
            con.commit()
            job_rows = con.execute("""
                SELECT job_id, company, title, url, dev_type, work_mode, location,
                       first_seen, last_seen, date_removed
                FROM job_index
                ORDER BY first_seen DESC, company, title
            """).fetchall()

        job_records = []
        for r in job_rows:
            job_id, company, title, url, dev_type, work_mode, location, \
                first_seen, last_seen, date_removed = r
            try:
                end_date = date_removed if date_removed else today_str
                days_listed = (date.fromisoformat(end_date) - date.fromisoformat(first_seen)).days
            except ValueError:
                days_listed = 0
            job_records.append({
                "jobId":       job_id,
                "company":     company,
                "title":       title,
                "url":         url,
                "devType":     dev_type,
                "workMode":    work_mode,
                "location":    location,
                "firstSeen":   first_seen,
                "lastSeen":    last_seen,
                "dateRemoved": date_removed,   # '' = still active
                "daysListed":  days_listed,
                "isActive":    date_removed == '',
            })
    else:
        # Fallback: job_index not yet populated (export before first full scrape)
        job_rows = con.execute(
            "SELECT snap_date, company, title, url, dev_type, work_mode, location, job_id "
            "FROM job_records ORDER BY snap_date DESC, company, title"
        ).fetchall()
        job_records = [
            {"jobId": r[7], "company": r[1], "title": r[2], "url": r[3],
             "devType": r[4], "workMode": r[5], "location": r[6],
             "firstSeen": r[0], "lastSeen": r[0], "dateRemoved": "",
             "daysListed": 0, "isActive": True}
            for r in job_rows
        ]

    # All companies ranked by latest-day count (for full ranking table in dashboard)
    all_ranking = sorted(
        [{"company": co, "cnt": by_co[co].get(latest_date, 0)} for co in by_co],
        key=lambda x: x["cnt"],
        reverse=True,
    )

    payload = {
        "generated":    datetime.now().isoformat(timespec="seconds"),
        "labels":       all_dates,
        "datasets":     datasets,
        "topCompanies": top_companies,
        "allRanking":   all_ranking,
        "latestDate":   latest_date,
        "latestTotal":  latest_total,
        "daysTracked":  len(all_dates),
        "jobRecords":   job_records,
    }

    DATA_PATH.write_text(
        "const JOB_DATA = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )
    print(
        f"Exported -> {DATA_PATH.name}  "
        f"({len(all_dates)} date(s), {len(top_companies)} companies, "
        f"{len(job_records)} job records)"
    )


# ── Company-targeted scraper ──────────────────────────────────────────────────

def fetch_jobs_company(filter_name: str) -> tuple[dict, list]:
    """
    Like fetch_jobs_israel() but server- AND client-filtered to one company name.
    Uses ?q=<name> for server-side pre-filtering; also skips cards whose company
    field doesn't contain filter_name (case-insensitive).
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    company_counts: dict[str, int] = {}
    job_list: list[dict] = []
    seen_ids: set[str] = set()
    consecutive_empty = 0
    name_lower = filter_name.lower()

    for page in range(1, MAX_PAGES + 1):
        try:
            resp = session.get(BASE_URL, params={"page": page, "q": filter_name}, timeout=20)
        except requests.RequestException as exc:
            print(f"  [page {page:3}]: network error - {exc}")
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            continue

        if resp.status_code == 429:
            print(f"  [page {page:3}]: rate-limited, waiting 60 s...")
            time.sleep(60)
            continue
        if resp.status_code != 200:
            print(f"  [page {page:3}]: HTTP {resp.status_code}")
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            continue

        soup  = BeautifulSoup(resp.text, "html.parser")
        grid  = soup.find("div", id="jobsGridList")
        cards = grid.find_all("div", class_="col-xl-4") if grid else []

        if not cards:
            print(f"  [page {page:3}]: no cards - stopping")
            break

        new = 0
        for card in cards:
            title_el = card.find("a", class_="name-job")
            co_el    = card.find("a", class_="profession")
            title  = title_el.get_text(strip=True) if title_el else ""
            co     = co_el.get_text(strip=True)    if co_el    else ""
            href   = title_el.get("href", "")      if title_el else ""
            job_id = href.rstrip("/").split("/")[-1] if href else ""

            if not co or not title or not job_id:
                continue
            if name_lower not in co.lower():          # client-side company filter
                continue
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            url = href if href.startswith("http") else f"https://devjobs.co.il{href}"

            loc_el = card.find("span", class_="location-small")
            location_full = loc_el.get_text(strip=True) if loc_el else ""
            m = re.match(r'^(.+?)\s*\(([^)]+)\)$', location_full)
            if m:
                location  = m.group(1).strip()
                work_mode = m.group(2).strip()
            else:
                location  = location_full
                work_mode = ""

            dev_type = classify_dev_type(title)
            company_counts[co] = company_counts.get(co, 0) + 1
            job_list.append({"company": co, "title": title, "url": url,
                             "dev_type": dev_type, "work_mode": work_mode,
                             "location": location, "job_id": job_id})
            new += 1

        total = sum(company_counts.values())
        print(f"  [page {page:3}]: +{new:2} new  (total: {total})")
        if new == 0:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                print("  3 empty pages in a row - stopping")
                break
        else:
            consecutive_empty = 0
        time.sleep(random.uniform(1.0, 2.0))

    print(f"  -> {len(company_counts)} company variant(s), {len(job_list)} jobs")
    return company_counts, job_list


def run_company(filter_name: str):
    """Scrape and store jobs for a single company; uses INSERT OR REPLACE to update
    existing records that have empty work_mode/location."""
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Collecting jobs for: {filter_name}")
    company_counts, job_list = fetch_jobs_company(filter_name)

    if not job_list:
        print(f"No jobs found for '{filter_name}'. Check spelling or try again later.")
        return

    today = date.today().isoformat()
    con   = init_db()

    # Update snapshots aggregation
    agg_rows = [(today, co, cnt) for co, cnt in company_counts.items() if cnt > 0]
    con.executemany(
        "INSERT OR REPLACE INTO snapshots(snap_date, company, cnt) VALUES (?,?,?)",
        agg_rows,
    )

    # INSERT OR REPLACE so existing rows get their work_mode/location updated
    rec_rows = [
        (today, j["company"], j["title"], j.get("url", ""),
         j.get("dev_type", ""), j.get("work_mode", ""), j.get("location", ""),
         j.get("job_id", ""))
        for j in job_list
    ]
    con.executemany(
        "INSERT OR REPLACE INTO job_records"
        "(snap_date, company, title, url, dev_type, work_mode, location, job_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        rec_rows,
    )
    con.commit()
    print(f"  Saved/updated {len(rec_rows)} {filter_name} records -> {DB_PATH.name}")
    update_job_index(con, job_list, today, mark_removals=False)
    export_data_js(con)
    con.close()
    print("Done.")


# ── Commands ──────────────────────────────────────────────────────────────────

def run_once():
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Collecting devjobs.co.il jobs in Israel ...")
    company_counts, job_list = fetch_jobs_israel()

    if not company_counts:
        print("No data fetched -- devjobs.co.il may be blocking. Try again later.")
        return

    today = date.today().isoformat()
    con   = init_db()
    save_snapshot(con, today, company_counts, job_list)
    update_job_index(con, job_list, today, mark_removals=True)
    export_data_js(con)
    con.close()
    print("Done.")


def cmd_status():
    con  = init_db()
    rows = con.execute(
        "SELECT snap_date, COUNT(*) AS companies, SUM(cnt) AS jobs "
        "FROM snapshots GROUP BY snap_date ORDER BY snap_date"
    ).fetchall()
    if not rows:
        print("DB is empty. Run without arguments to collect data.")
    else:
        print(f"{'Date':<12}  {'Companies':>10}  {'Jobs sampled':>13}")
        print("-" * 40)
        for snap_date, companies, jobs in rows:
            print(f"{snap_date:<12}  {companies:>10}  {jobs:>13}")
    con.close()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"

    if cmd == "export":
        export_data_js(init_db())
    elif cmd == "status":
        cmd_status()
    elif cmd == "company":
        if len(sys.argv) < 3:
            sys.exit("Usage: jobs_tracker.py company <CompanyName>")
        run_company(sys.argv[2])
    elif cmd == "loop":
        print(f"Loop mode -- collecting every {POLL_HOURS} h. Ctrl-C to stop.")
        while True:
            run_once()
            print(f"  Sleeping {POLL_HOURS} h ...\n")
            time.sleep(POLL_HOURS * 3600)
    else:
        run_once()


if __name__ == "__main__":
    main()
