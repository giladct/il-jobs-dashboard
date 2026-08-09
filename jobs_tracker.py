#!/usr/bin/env python3
"""
Israel Dev Jobs Tracker — devjobs.co.il + lin-srael.com edition
Scrapes devjobs.co.il (Israeli tech-focused job board, ~3,170 listings) and
lin-srael.com (tech-classified subset of its LinkedIn-Israel job feed) for
all open positions.  Aggregates by company name and stores daily snapshots to
SQLite; exports data.js for the Chart.js dashboard.

Free — no API key required.
Requires: pip install requests beautifulsoup4

Usage:
  python jobs_tracker.py                   # collect one snapshot now (devjobs + lin-srael) + export data.js
  python jobs_tracker.py export            # re-export data.js from existing DB
  python jobs_tracker.py loop             # collect daily in a blocking loop
  python jobs_tracker.py status           # print DB summary
  python jobs_tracker.py company <Name>   # scrape + update one company only (fast test, devjobs.co.il)
  python jobs_tracker.py linsrael         # standalone lin-srael.com scrape (no removal marking)

Notes:
  - Paginates up to MAX_PAGES pages (30 jobs/page) through devjobs.co.il.
  - robots.txt is fully permissive; a polite 1-2 s delay is added between pages.
  - lin-srael.com jobs are merged into job_index by job_id (same ID space as
    LinkedIn/devjobs); a job seen by both sources is tagged source='both'.
  - Run once a day (or via Windows Task Scheduler) to build trend history.
"""

import json
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, date, timedelta
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
    ("Full Stack", ["full stack", "fullstack", "full-stack", "software dev ", " sw ", "sw engineer",
                    "sw developer", "software architect", "solutions architect", "technical architect",
                    " architect"]),
    ("Frontend",   ["frontend", "front end", "front-end", "react ", "vue ", "angular ",
                    "ui developer", "ui/ux"]),
    ("Backend",    ["backend", "back end", "back-end", "java ", "python ", "node.js",
                    "django", "spring", ".net ", "golang", "ruby", "php "]),
    ("DevOps",     ["devops", "dev ops", "sre", "infrastructure", "cloud engineer",
                    "platform engineer", "kubernetes", "docker", "linux", "finops", "hpc"]),
    ("Data/ML",    ["data scientist", "data engineer", "machine learning", "ml engineer",
                    "data analyst", "ai ", " ml ", "deep learning", "nlp", "llm",
                    "algorithm", "algo ", "computer vision", "artificial intellig",
                    "dataops", "bi ", "bi engineer", "dwh", "data science",
                    "business intelligence"]),
    ("Mobile",     ["mobile", " ios ", "android", "react native", "flutter", "swift", "kotlin"]),
    ("QA",         ["qa ", "quality assurance", "automation engineer", "sdet", "tester",
                    "validation engineer", "quality engineer", "devtest"]),
    ("Security",   ["security", "cybersecurity", "pentest", "soc ", "appsec", "exploit"]),
    ("Manager",    ["manager", "team lead", "vp ", "director", "cto", "head of", "r&d lead"]),
    ("Embedded",   ["embedded", "firmware", "fpga", "vhdl", "verilog", "rtos",
                    "asic", "vlsi", "chip design", "dft ", " dv ", "design verif",
                    "verification engineer", "silicon valid", "board design", "analog design",
                    "mixed signal", "power electron", " rf ", "rf engineer", "wifi", "wi-fi",
                    "wireless eng", "compiler", "electrical eng", "emulation", " hw ",
                    "hw/", "ate ", "physical design", "cellular", "low level", "bsp ",
                    "control eng", "navigation eng", "motion control",
                    " cpu ", "cpu ", " cad ", "cad ", "logic design", "gate array",
                    "post-silicon", "post silicon", " gnc ", "guidance eng", "kernel"]),
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
            id          INTEGER PRIMARY KEY,
            snap_date   TEXT NOT NULL,
            company     TEXT NOT NULL,
            title       TEXT NOT NULL,
            url         TEXT,
            dev_type    TEXT DEFAULT '',
            work_mode   TEXT DEFAULT '',
            location    TEXT DEFAULT '',
            job_id      TEXT DEFAULT '',
            posted_date TEXT DEFAULT '',
            UNIQUE(snap_date, company, title)
        )
    """)
    # Migrate older DBs: add columns if they don't exist yet
    for col, dflt in [("dev_type", "''"), ("work_mode", "''"), ("location", "''"), ("job_id", "''"), ("posted_date", "''")]:
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
            date_removed TEXT DEFAULT '',
            posted_date  TEXT DEFAULT ''
        )
    """)
    for col, coltype, dflt in [("posted_date", "TEXT", "''"), ("linkedin_posted", "TEXT", "''"),
                               ("linkedin_applicants", "TEXT", "''"), ("linkedin_checked", "TEXT", "''"),
                               ("linkedin_applicant_n", "INTEGER", "NULL"),
                               ("source", "TEXT", "'devjobs'")]:
        try:
            con.execute(f"ALTER TABLE job_index ADD COLUMN {col} {coltype} DEFAULT {dflt}")
        except Exception:
            pass

    # Bootstrap job_index from job_records history (runs once when job_index is empty)
    ji_count = con.execute("SELECT COUNT(*) FROM job_index").fetchone()[0]
    jr_count  = con.execute("SELECT COUNT(*) FROM job_records WHERE job_id != ''").fetchone()[0]
    if ji_count == 0 and jr_count > 0:
        con.execute("""
            INSERT OR IGNORE INTO job_index
                (job_id, company, title, url, dev_type, work_mode, location,
                 first_seen, last_seen, date_removed, source)
            SELECT jr.job_id, jr.company, jr.title, jr.url,
                   jr.dev_type, jr.work_mode, jr.location,
                   agg.first_seen, agg.last_seen, '', 'devjobs'
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

    con.execute("DROP TABLE IF EXISTS job_appearances")

    # repost_events: one row per close->reopen cycle for a given job_id (same
    # LinkedIn job ID reappearing after being marked removed). Logged by
    # upsert_job_index() at the moment it detects the reopen.
    con.execute("""
        CREATE TABLE IF NOT EXISTS repost_events (
            id            INTEGER PRIMARY KEY,
            job_id        TEXT NOT NULL,
            company       TEXT NOT NULL,
            title         TEXT NOT NULL,
            closed_date   TEXT NOT NULL,
            reopened_date TEXT NOT NULL,
            gap_days      INTEGER NOT NULL
        )
    """)

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
         j.get("job_id", ""), j.get("posted_date", ""))
        for j in job_list
    ]
    con.executemany(
        "INSERT OR IGNORE INTO job_records"
        "(snap_date, company, title, url, dev_type, work_mode, location, job_id, posted_date)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        rec_rows,
    )
    con.commit()
    print(f"  Saved {len(agg_rows)} companies, {len(rec_rows)} job records -> {DB_PATH.name}")


def upsert_job_index(con: sqlite3.Connection, job_list: list, today_date: str,
                     source: str = "devjobs") -> set[str]:
    """
    Upsert today's scraped jobs into job_index (one row per unique job), tagging
    each with `source` ('devjobs' or 'linsrael'). A job seen by both sources
    (same job_id — devjobs.co.il job IDs are LinkedIn job IDs, same ID space as
    lin-srael's linkedin_job_id) gets source='both'.

    `source='devjobs'` is treated as authoritative for identity fields (company,
    title, url, dev_type, work_mode, location, posted_date) since devjobs.co.il's
    structured cards parse more precisely than lin-srael's free-text listing —
    a devjobs upsert always overwrites these fields, matching pre-existing
    behavior. `source='linsrael'` only fills those fields in when the row wasn't
    already seen by devjobs, so it never clobbers devjobs' richer data; it still
    always refreshes last_seen / clears date_removed / merges the source tag.

    Returns the set of job_ids upserted (this source's "seen today" set), for
    use by mark_removed_jobs().
    """
    assert source in ("devjobs", "linsrael")
    today_ids = {j["job_id"] for j in job_list if j.get("job_id")}
    if not today_ids:
        return today_ids

    # Repost detection: any of today's job_ids that are currently marked
    # removed are being reopened right now — log the close->reopen cycle
    # before the upsert below clears date_removed. Runs before either
    # source's upsert in a given session, so only the first source to see
    # the job_id today claims the event (the other's date_removed is
    # already '' by the time it runs).
    placeholders = ",".join("?" * len(today_ids))
    reopened = con.execute(f"""
        SELECT job_id, company, title, date_removed FROM job_index
        WHERE job_id IN ({placeholders}) AND date_removed != ''
    """, tuple(today_ids)).fetchall()
    if reopened:
        repost_rows = []
        for job_id, company, title, closed_date in reopened:
            try:
                gap_days = (date.fromisoformat(today_date) - date.fromisoformat(closed_date)).days
            except ValueError:
                gap_days = 0
            repost_rows.append((job_id, company, title, closed_date, today_date, gap_days))
        con.executemany("""
            INSERT INTO repost_events (job_id, company, title, closed_date, reopened_date, gap_days)
            VALUES (?,?,?,?,?,?)
        """, repost_rows)

    upsert_rows = [
        (j["job_id"], j["company"], j["title"], j.get("url", ""),
         j.get("dev_type", ""), j.get("work_mode", ""), j.get("location", ""),
         today_date, today_date, j.get("posted_date", ""), source)
        for j in job_list if j.get("job_id")
    ]

    # devjobs is authoritative (always wins); linsrael is supplementary (only
    # wins when the existing row wasn't already seen by devjobs) — express
    # both as one templated CASE per identity field rather than two hand-
    # written copies.
    winner_cond   = "1=1" if source == "devjobs" else "job_index.source = 'linsrael'"
    other_sources = "'linsrael','both'" if source == "devjobs" else "'devjobs','both'"
    source_merge  = f"CASE WHEN job_index.source IN ({other_sources}) THEN 'both' ELSE '{source}' END"

    NONEMPTY_GUARDED = {"work_mode", "location", "posted_date"}  # only overwrite when incoming value is non-empty
    field_updates = ",\n            ".join(
        f"{col} = CASE WHEN {winner_cond}"
        + (f" AND excluded.{col} != ''" if col in NONEMPTY_GUARDED else "")
        + f" THEN excluded.{col} ELSE job_index.{col} END"
        for col in ("company", "title", "url", "dev_type", "work_mode", "location", "posted_date")
    )

    con.executemany(f"""
        INSERT INTO job_index
            (job_id, company, title, url, dev_type, work_mode, location,
             first_seen, last_seen, date_removed, posted_date, source)
        VALUES (?,?,?,?,?,?,?,?,?,'',?,?)
        ON CONFLICT(job_id) DO UPDATE SET
            last_seen    = excluded.last_seen,
            date_removed = '',
            {field_updates},
            source       = {source_merge}
    """, upsert_rows)
    return today_ids


def mark_removed_jobs(con: sqlite3.Connection, today_date: str, today_ids: set[str],
                       source_filter: str | None = None):
    """
    Marks date_removed for any active job_index row whose job_id is absent from
    `today_ids`. `today_ids` should be the UNION of every source's "seen today"
    set for sources that actually ran this session — passing only one source's
    ids here would wrongly mark jobs the other source still sees as removed.

    `source_filter`, if given, restricts the update to rows whose `source`
    column exactly matches it (e.g. 'linsrael'). This is what makes a
    single-source run safe to mark removals with: we have full historical
    visibility over jobs exclusively tagged with that source (we're the only
    ones who ever reported them), so their disappearance from today's scrape
    is a real removal signal — unlike 'devjobs'/'both' rows, which a lin-srael-
    only scrape can't judge (lin-srael's tech subset doesn't cover devjobs'
    full universe, so absence there doesn't mean the job is gone).
    """
    if not today_ids:
        print("  WARNING: mark_removed_jobs called with empty today_ids — skipping.")
        return
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _today_ids (job_id TEXT PRIMARY KEY)")
    con.execute("DELETE FROM _today_ids")
    con.executemany("INSERT INTO _today_ids VALUES (?)", [(jid,) for jid in today_ids])
    source_clause = "AND source = ?" if source_filter else ""
    params = (today_date,) + ((source_filter,) if source_filter else ())
    con.execute(f"""
        UPDATE job_index
        SET date_removed = ?
        WHERE date_removed = ''
          AND job_id NOT IN (SELECT job_id FROM _today_ids)
          {source_clause}
    """, params)
    con.execute("DROP TABLE IF EXISTS _today_ids")


def _log_job_index_summary(con: sqlite3.Connection, today_date: str, upserted_label: str):
    """Prints the standard 'N upserted, M removed today, K active total' line."""
    removed_today = con.execute(
        "SELECT COUNT(*) FROM job_index WHERE date_removed = ?", (today_date,)
    ).fetchone()[0]
    active = con.execute(
        "SELECT COUNT(*) FROM job_index WHERE date_removed = ''"
    ).fetchone()[0]
    print(f"  job_index: {upserted_label}, {removed_today} removed today, {active} active total.")


def update_job_index(con: sqlite3.Connection, job_list: list, today_date: str,
                     mark_removals: bool = True, source: str = "devjobs"):
    """Convenience wrapper: upsert a single source's jobs, optionally marking
    removals against that same source's today_ids (safe for single-source runs
    like `company`/standalone `linsrael`; `run_once` calls the two pieces
    directly so it can mark removals against the UNION of both sources)."""
    today_ids = upsert_job_index(con, job_list, today_date, source=source)
    if mark_removals:
        mark_removed_jobs(con, today_date, today_ids)
    con.commit()
    _log_job_index_summary(con, today_date, f"{len(today_ids)} upserted ({source})")


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

            # ── Posted date (span.card-time: "Jun 03, 2026") ──────────────
            time_el = card.find("span", class_="card-time")
            try:
                posted_date = datetime.strptime(
                    time_el.get_text(strip=True), "%b %d, %Y"
                ).date().isoformat() if time_el else ""
            except ValueError:
                posted_date = ""

            # ── Developer type (derived from job title) ────────────────────
            dev_type = classify_dev_type(title)

            company_counts[co] = company_counts.get(co, 0) + 1
            job_list.append({"company": co, "title": title, "url": url,
                             "dev_type": dev_type, "work_mode": work_mode,
                             "location": location, "job_id": job_id,
                             "posted_date": posted_date})
            new += 1

        total = sum(company_counts.values())
        print(f"  [page {page:3}]: +{new:2} new  (total: {total})")
        time.sleep(random.uniform(1.0, 2.0))

    print(f"  -> {len(company_counts)} unique companies, {len(job_list)} unique jobs")
    return company_counts, job_list


# ── lin-srael.com scraper ───────────────────────────────────────────────────────
# lin-srael.com ("LinSrael Insights") is a Base44-built site that pulls Israel
# job postings straight from LinkedIn's public search. It's a much broader crawl
# than devjobs.co.il (~12,000 postings across every profession vs. devjobs'
# tech-only ~3,000), so we only pull its tech-classified subset here, matching
# this dashboard's scope. Its `linkedin_job_id` is the same ID space as
# devjobs.co.il job IDs (both ultimately point at LinkedIn job postings), which
# is what lets upsert_job_index() merge duplicates by job_id.

LINSRAEL_APP_ID = "6a06c46d3864156006253ad5"
LINSRAEL_SEARCH_URL = f"https://lin-srael.com/api/apps/{LINSRAEL_APP_ID}/functions/searchJobs"
LINSRAEL_PAGE_LIMIT = 100

# Only the tech/engineering classifications lin-srael exposes — it also carries
# a huge volume of non-tech postings (Sales, Marketing, Finance, HR, ...) that
# are out of scope for this dashboard.
LINSRAEL_TECH_CLASSIFICATIONS = [
    "Backend Engineering", "DevOps", "Embedded, Low Level & Firmware Engineering",
    "Frontend Engineering", "Mobile Development", "Data Science, ML & Algorithms",
    "AI Engineering", "Fullstack Engineering", "Systems Engineering",
    "Hardware Engineering", "QA", "QA Automation", "Cybersecurity",
    "IT and System Administration", "UI/UX, Design & Content", "Data Analyst",
]

LINSRAEL_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Content-Type": "application/json",
}


def fetch_jobs_linsrael() -> tuple[dict, list]:
    """
    Pulls all tech-classified job postings from lin-srael.com's public
    (auth-free) searchJobs function, paginated per classification.
    Returns ({company: count}, [{company, title, url, dev_type, work_mode,
    location, job_id, posted_date}, ...]) deduplicated by linkedin_job_id.
    """
    session = requests.Session()
    session.headers.update(LINSRAEL_HEADERS)

    company_counts: dict[str, int] = {}
    job_list: list[dict] = []
    seen_ids: set[str] = set()

    for classification in LINSRAEL_TECH_CLASSIFICATIONS:
        page = 1
        pages = 1
        while page <= pages:
            body = {
                "title": "", "company_name": "", "location": "", "experience_level": "",
                "work_type": "", "sector": "", "description": "",
                "job_classification": classification,
                "page": page, "limit": LINSRAEL_PAGE_LIMIT,
            }
            try:
                resp = session.post(LINSRAEL_SEARCH_URL, json=body, timeout=20)
            except requests.RequestException as exc:
                print(f"  [{classification} p{page}]: network error - {exc}")
                break

            if resp.status_code == 429:
                print(f"  [{classification} p{page}]: rate-limited, waiting 30 s...")
                time.sleep(30)
                continue
            if resp.status_code != 200:
                print(f"  [{classification} p{page}]: HTTP {resp.status_code}")
                break

            data = resp.json()
            pages = data.get("pages", 1)
            jobs = data.get("jobs", [])

            for j in jobs:
                job_id = str(j.get("linkedin_job_id") or "")
                co     = (j.get("company_name") or "").strip()
                title  = (j.get("title") or "").strip()
                if not co or not title or not job_id:
                    continue
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                # published_at is usually 'YYYY-MM-DD' but occasionally a full
                # ISO timestamp — normalize to a bare date either way.
                posted_date = (j.get("published_at") or "")[:10]

                company_counts[co] = company_counts.get(co, 0) + 1
                job_list.append({
                    "company": co, "title": title,
                    "url": j.get("job_url") or j.get("apply_url") or "",
                    "dev_type": classify_dev_type(title),
                    "work_mode": "",   # lin-srael doesn't expose remote/hybrid/on-site in results
                    "location": j.get("location") or "",
                    "job_id": job_id,
                    "posted_date": posted_date,
                })

            print(f"  [{classification}] page {page}/{pages}: +{len(jobs)} (total so far: {len(job_list)})")
            page += 1
            time.sleep(random.uniform(0.4, 0.8))

    print(f"  -> {len(company_counts)} unique companies, {len(job_list)} unique jobs (lin-srael, tech only)")
    return company_counts, job_list


# ── LinkedIn enrichment ────────────────────────────────────────────────────────

LINKEDIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

def fetch_linkedin_meta(job_id: str) -> tuple[str, str]:
    """
    Fetches the public LinkedIn job-view page (devjobs.co.il job IDs are LinkedIn
    job IDs) and scrapes the posted-time-ago and applicant-count text.
    Returns ('', '') on any failure (404, blocked, layout change, etc).
    """
    try:
        resp = requests.get(
            f"https://www.linkedin.com/jobs/view/{job_id}/",
            headers=LINKEDIN_HEADERS, timeout=15,
        )
    except requests.RequestException as exc:
        print(f"    {job_id}: network error - {exc}")
        return "", ""

    if resp.status_code != 200:
        print(f"    {job_id}: HTTP {resp.status_code}")
        return "", ""

    soup = BeautifulSoup(resp.text, "html.parser")
    posted     = soup.select_one(".posted-time-ago__text")
    applicants = soup.select_one(".num-applicants__caption")
    return (
        posted.get_text(strip=True) if posted else "",
        applicants.get_text(strip=True) if applicants else "",
    )


def parse_applicant_count(applicants_text: str) -> int | None:
    """'111 applicants' -> 111, 'Be among the first 25 applicants' -> 25, '' -> None."""
    m = re.search(r"(\d+)", applicants_text or "")
    return int(m.group(1)) if m else None


LINKEDIN_OFFSET_DAYS  = 2      # only jobs first seen exactly this many days ago are eligible
LINKEDIN_APPLICANT_CAP = 100   # once a job's applicant count reaches this, stop re-polling it
LINKEDIN_DAILY_LIMIT  = 150    # max LinkedIn requests per run


def _run_linkedin_batch(con: sqlite3.Connection, rows: list[tuple[str]]):
    """Fetches LinkedIn data for the given job_id rows and updates job_index."""
    if not rows:
        print("No jobs matched the LinkedIn enrichment criteria.")
        return
    print(f"Fetching LinkedIn data for {len(rows)} job(s)...")
    today_str = date.today().isoformat()
    ok = 0
    for i, (job_id,) in enumerate(rows, 1):
        posted, applicants = fetch_linkedin_meta(job_id)
        applicant_n = parse_applicant_count(applicants)
        con.execute(
            "UPDATE job_index SET linkedin_posted=?, linkedin_applicants=?, "
            "linkedin_applicant_n=?, linkedin_checked=? WHERE job_id=?",
            (posted, applicants, applicant_n, today_str, job_id),
        )
        con.commit()
        status = "OK" if (posted or applicants) else "no data"
        print(f"  [{i:3}/{len(rows)}] {job_id}: {posted!r} | {applicants!r} ({status})")
        if posted or applicants:
            ok += 1
        time.sleep(random.uniform(1.5, 3.0))
    print(f"Done. {ok}/{len(rows)} jobs got LinkedIn data.")


def cmd_linkedin(limit: int):
    """Manual/ad-hoc: enrich up to `limit` active jobs (oldest-checked first), no filters."""
    con = init_db()
    rows = con.execute("""
        SELECT job_id FROM job_index
        WHERE date_removed = ''
        ORDER BY linkedin_checked = '' DESC, linkedin_checked ASC
        LIMIT ?
    """, (limit,)).fetchall()
    _run_linkedin_batch(con, rows)
    export_data_js(con)
    con.close()


def cmd_linkedin_data():
    """
    Manual-run policy: all jobs first seen exactly LINKEDIN_OFFSET_DAYS days ago
    (any dev_type), capped at LINKEDIN_APPLICANT_CAP applicants (stop polling
    once reached), at most LINKEDIN_DAILY_LIMIT requests, oldest-checked first.
    """
    con = init_db()
    target_date = (date.today() - timedelta(days=LINKEDIN_OFFSET_DAYS)).isoformat()
    rows = con.execute("""
        SELECT job_id FROM job_index
        WHERE date_removed = ''
          AND first_seen = ?
          AND (linkedin_applicant_n IS NULL OR linkedin_applicant_n < ?)
        ORDER BY linkedin_checked = '' DESC, linkedin_checked ASC
        LIMIT ?
    """, (target_date, LINKEDIN_APPLICANT_CAP, LINKEDIN_DAILY_LIMIT)).fetchall()
    _run_linkedin_batch(con, rows)
    export_data_js(con)
    con.close()


# ── Export ────────────────────────────────────────────────────────────────────

def export_data_js(con: sqlite3.Connection):
    rows = con.execute(
        "SELECT snap_date, company, cnt FROM snapshots ORDER BY snap_date, cnt DESC"
    ).fetchall()

    if not rows:
        print("No data in DB yet -- run without arguments first to collect a snapshot.")
        return

    # All dates in order — always include today. The "over time" charts compute
    # each date's state live from job_index/jobRecords (first_seen/date_removed),
    # not from `snapshots`, so today belongs on the axis even when only one
    # source's scraper ran today (e.g. devjobs.co.il was down but lin-srael ran).
    all_dates = sorted(set(r[0] for r in rows) | {date.today().isoformat()})

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

    # Repost history per job_id, for the raw table's "Reposts" column/tooltip
    reposts_by_job = {}
    for job_id, closed_date, reopened_date, gap_days in con.execute("""
        SELECT job_id, closed_date, reopened_date, gap_days
        FROM repost_events ORDER BY job_id, reopened_date
    """).fetchall():
        reposts_by_job.setdefault(job_id, []).append({
            "closedDate": closed_date, "reopenedDate": reopened_date, "gapDays": gap_days,
        })

    if ji_count > 0:
        job_rows = con.execute("""
            SELECT job_id, company, title, url, dev_type, work_mode, location,
                   first_seen, last_seen, date_removed, posted_date,
                   linkedin_posted, linkedin_applicants, linkedin_applicant_n, source
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
                       first_seen, last_seen, date_removed, posted_date,
                       linkedin_posted, linkedin_applicants, linkedin_applicant_n, source
                FROM job_index
                ORDER BY first_seen DESC, company, title
            """).fetchall()

        job_records = []
        for r in job_rows:
            job_id, company, title, url, dev_type, work_mode, location, \
                first_seen, last_seen, date_removed, posted_date, \
                linkedin_posted, linkedin_applicants, linkedin_applicant_n, source = r
            try:
                end_date = date_removed if date_removed else today_str
                days_listed = (date.fromisoformat(end_date) - date.fromisoformat(first_seen)).days
            except ValueError:
                days_listed = 0
            job_records.append({
                "jobId":        job_id,
                "company":      company,
                "title":        title,
                "url":          url,
                "devType":      dev_type,
                "workMode":     work_mode,
                "location":     location,
                "firstSeen":    first_seen,
                "lastSeen":     last_seen,
                "dateRemoved":  date_removed,   # '' = still active
                "daysListed":   days_listed,
                "isActive":     date_removed == '',
                "postedDate":   posted_date,
                "linkedinPosted":     linkedin_posted,
                "linkedinApplicants": linkedin_applicants,
                "linkedinApplicantN": linkedin_applicant_n,
                "source":       source or "devjobs",   # 'devjobs' | 'linsrael' | 'both'
                "repostCount":   len(reposts_by_job.get(job_id, [])),
                "repostHistory": reposts_by_job.get(job_id, []),
            })
    else:
        # Fallback: job_index not yet populated (export before first full scrape)
        job_rows = con.execute(
            "SELECT snap_date, company, title, url, dev_type, work_mode, location, job_id "
            "FROM job_records ORDER BY snap_date DESC, company, title"
        ).fetchall()
        job_records = [
            {"jobId": r[7], "company": r[1], "title": r[2], "url": r[3],
             "source": "devjobs",
             "devType": r[4], "workMode": r[5], "location": r[6],
             "firstSeen": r[0], "lastSeen": r[0], "dateRemoved": "",
             "daysListed": 0, "isActive": True,
             "repostCount": 0, "repostHistory": []}
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

            time_el = card.find("span", class_="card-time")
            try:
                posted_date = datetime.strptime(
                    time_el.get_text(strip=True), "%b %d, %Y"
                ).date().isoformat() if time_el else ""
            except ValueError:
                posted_date = ""

            dev_type = classify_dev_type(title)
            company_counts[co] = company_counts.get(co, 0) + 1
            job_list.append({"company": co, "title": title, "url": url,
                             "dev_type": dev_type, "work_mode": work_mode,
                             "location": location, "job_id": job_id,
                             "posted_date": posted_date})
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
         j.get("job_id", ""), j.get("posted_date", ""))
        for j in job_list
    ]
    con.executemany(
        "INSERT OR REPLACE INTO job_records"
        "(snap_date, company, title, url, dev_type, work_mode, location, job_id, posted_date)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
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
    devjobs_ids = upsert_job_index(con, job_list, today, source="devjobs")

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Collecting lin-srael.com jobs (tech only) ...")
    try:
        _, linsrael_job_list = fetch_jobs_linsrael()
    except Exception as exc:
        print(f"  lin-srael scrape failed, continuing with devjobs data only: {exc}")
        linsrael_job_list = []
    linsrael_ids = upsert_job_index(con, linsrael_job_list, today, source="linsrael") if linsrael_job_list else set()

    # Mark removals against the UNION of both sources' today-ids — a job absent
    # from devjobs but still seen by lin-srael (or vice versa) must stay active.
    mark_removed_jobs(con, today, devjobs_ids | linsrael_ids)
    con.commit()
    _log_job_index_summary(con, today, f"{len(devjobs_ids)} devjobs + {len(linsrael_ids)} lin-srael upserted")

    export_data_js(con)
    con.close()
    print("Done.")


def cmd_linsrael():
    """Standalone lin-srael.com scrape — upserts into job_index (merging with any
    matching devjobs job_ids) and marks removals for source='linsrael'-only rows
    (jobs we've only ever seen via lin-srael, so its own history is a complete
    record for them). 'devjobs'/'both' rows are left untouched — lin-srael's
    tech subset doesn't cover devjobs' full universe, so their absence here
    isn't a reliable removal signal. Safe to run anytime."""
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Collecting lin-srael.com jobs (tech only) ...")
    _, job_list = fetch_jobs_linsrael()
    if not job_list:
        print("No data fetched -- lin-srael.com may be blocking or its API may have changed.")
        return
    today = date.today().isoformat()
    con = init_db()
    today_ids = upsert_job_index(con, job_list, today, source="linsrael")
    mark_removed_jobs(con, today, today_ids, source_filter="linsrael")
    con.commit()
    _log_job_index_summary(con, today, f"{len(job_list)} lin-srael jobs upserted")
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
    elif cmd == "linkedin":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 15
        cmd_linkedin(limit)
    elif cmd == "linkedin-data":
        cmd_linkedin_data()
    elif cmd == "linsrael":
        cmd_linsrael()
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
