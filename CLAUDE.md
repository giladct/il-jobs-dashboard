# Israel Jobs Dashboard — Project Context

## What this is
A scraper + Chart.js dashboard tracking open tech jobs on **devjobs.co.il** (Israeli job board, ~3,100 listings).
Daily snapshots go to SQLite; a static `data.js` feeds the `index.html` dashboard. No server needed — open `index.html` directly.

---

## Files

| File | Purpose |
|------|---------|
| `jobs_tracker.py` | Scraper, DB, exporter — the only Python file |
| `jobs.db` | SQLite database — **tracked in git** so GitHub Actions never loses history |
| `data.js` | Generated JS payload for dashboard (`const JOB_DATA = {...}`) |
| `index.html` | Chart.js dashboard — open directly in browser |

---

## CLI commands

```
py jobs_tracker.py                   # full scrape + export (takes ~3-4 min)
py jobs_tracker.py export            # re-export data.js from existing DB (no scrape)
py jobs_tracker.py company <Name>    # targeted single-company scrape (fast test)
py jobs_tracker.py status            # print snapshot summary table
py jobs_tracker.py loop              # run every 24 h (blocking)
py jobs_tracker.py linkedin [N]      # manual: enrich N active jobs (default 15) with LinkedIn posted-time/applicants, no filters
py jobs_tracker.py linkedin-data     # automated daily policy: Data/ML jobs only, first_seen >= launch date, capped at 100 applicants, max 99/day
```

**Windows Task Scheduler** runs the full scrape daily automatically.
**GitHub Actions** (`.github/workflows/daily-scrape.yml`) also runs daily at 06:00 UTC and commits both `data.js` and `jobs.db` back to the repo. `jobs.db` is tracked in git (not in .gitignore) so historical snapshots are never lost if the runner restarts.

---

## DB schema

```sql
snapshots    (id, snap_date, company, cnt)
             -- aggregated daily counts; UNIQUE(snap_date, company)

job_records  (id, snap_date, company, title, url,
              dev_type, work_mode, location, job_id)
             -- one row per job per day; UNIQUE(snap_date, company, title)
             -- job_id extracted from URL path (e.g. "4417518027")

job_index    (job_id PK, company, title, url,
              dev_type, work_mode, location,
              first_seen, last_seen, date_removed,
              linkedin_posted, linkedin_applicants,
              linkedin_applicant_n, linkedin_checked)
             -- one canonical row per unique job
             -- date_removed = '' means still active
             -- source of truth for the dashboard's raw table
             -- job_id doubles as the LinkedIn job ID (devjobs.co.il sources from LinkedIn,
             --   so https://www.linkedin.com/jobs/view/{job_id}/ resolves directly)
             -- linkedin_* columns populated by `linkedin` / `linkedin-data` commands only
```

### job_index lifecycle
- **Full scrape** (`run_once`): upserts all today's jobs; marks absent active jobs with `date_removed = today`
- **Company scrape** (`run_company`): upserts that company's jobs only; never marks removals
- **Re-listed job**: `date_removed` cleared back to `''`, `first_seen` preserved
- **Bootstrap**: on first run after adding `job_index`, auto-populated from `job_records` history

---

## Scraper — key implementation details

### devjobs.co.il HTML structure
- Cards live inside `<div id="jobsGridList">` → child `<div class="col-xl-4">` elements
- Job title: `<a class="name-job">` (also has `href` with job ID)
- Company: `<a class="profession">`
- **Location + work mode combined** in `<span class="location-small">`:
  - Format: `"Tel Aviv-Yafo (Hybrid)"` / `"Israel (Remote)"` / `"Be'er Sheva (On-site)"`
  - Parsed with regex: `r'^(.+?)\s*\(([^)]+)\)$'`
  - Work modes seen: `On-site`, `Hybrid`, `Remote`

### Pagination
- `GET https://devjobs.co.il/jobs-grid?page=N`
- `?q=<term>` does server-side keyword filtering (used by `company` command)
- ~106 real pages × 30 cards; stops on 3 consecutive empty pages
- 1–2 s polite delay between pages

### Developer type classification
Keyword matching on job title → 10 categories:
`Full Stack`, `Frontend`, `Backend`, `DevOps`, `Data/ML`, `Mobile`, `QA`, `Security`, `Manager`, `Embedded`, `Other`
Always populated (derived from title, backfilled on export for old rows).

---

## data.js payload structure

```json
{
  "generated": "2026-05-24T...",
  "labels": ["2026-05-23", ...],          // all snapshot dates
  "datasets": [...],                       // top 30 companies for line chart
  "topCompanies": [...],                   // top 30 company names (for chips)
  "allRanking": [{"company":"NVIDIA","cnt":478}, ...],  // ALL companies sorted by count
  "latestDate": "2026-05-24",
  "latestTotal": 3135,
  "daysTracked": 2,
  "jobRecords": [{
    "jobId", "company", "title", "url",
    "devType", "workMode", "location",
    "firstSeen", "lastSeen",
    "dateRemoved",   // '' = active, 'YYYY-MM-DD' = removed
    "daysListed",    // days from firstSeen to dateRemoved (or today if active)
    "isActive"       // boolean
  }, ...]
}
```

---

## Dashboard — index.html features

### Stats bar
Jobs in snapshot · Companies · Days tracked · Latest snapshot date

### Filter bar 1 — Company chips
Top-30 company chips (colored) to toggle chart lines. Buttons: All / None / Top 10.

### Filter bar 2 — Metadata filters
- **Role** dropdown (devType: Full Stack, Backend, etc.)
- **Mode** dropdown (workMode: Hybrid / On-site / Remote)
- **Location** dropdown (location: city names)
- **Title** text input (keyword search on title only)
- **Listed** dropdown (filter by `firstSeen` date)
- **Status** dropdown — "Active now" shows only jobs with `dateRemoved = ''`; specific dates show jobs removed on that day
- **Raw search** box (matches company OR title)

All filters combine with AND logic.

### Line chart
Open positions over time, one line per company (only `datasets` = top 30 shown).

### Ranking table
All companies sorted by latest-day job count — scrollable, max-height 420 px.

### Raw data table
One row per unique job (from `job_index`). Columns: First Seen / Company / Job Title / Role (badge) / Mode / Location / Days / Removed / Link.
- **Days** — how long the job was/has been listed (tooltip shows exact first/last seen dates)
- **Removed** — green "active" or red removal date
Sortable by any column. Shows filtered count vs total.

---

## Pending / known issues

1. **work_mode and location empty for 2026-05-23 records** — can't be backfilled (data not captured).
2. **Intel has no current listings** on devjobs.co.il (verified 2026-05-24).
3. **TOP_N = 30** — line chart only shows top 30 companies. Ranking table shows all.
4. **LinkedIn enrichment (`linkedin-data`)** covers all jobs (any `dev_type`) first seen exactly `LINKEDIN_OFFSET_DAYS` (2) days ago. Polling for a job stops once its applicant count reaches 100 (`LINKEDIN_APPLICANT_CAP`). Capped at `LINKEDIN_DAILY_LIMIT` (99) requests/run. **Run manually only** (`py jobs_tracker.py linkedin-data`) — not part of the daily GitHub Actions workflow, and not run automatically/proactively by Claude.
5. **LinkedIn applicant count is the older "applicants" widget, not what you see logged in.** Our scraper hits the public/logged-out HTML, which only exposes `.num-applicants__caption` (e.g. "Over 200 applicants"). LinkedIn's logged-in UI now shows a different, separate metric — "X people clicked apply" — especially for jobs with "Responses managed off LinkedIn" (external ATS), where LinkedIn can't count real applicants and only counts Apply-button clicks. That text isn't present in the anonymous HTML at all, so the dashboard's number can be stale or simply a different metric than what you see browsing LinkedIn directly. Also note: "Over N applicants"/"Over 200 applicants" is a floor, not exact — `parse_applicant_count()` just extracts the first number, so treat values ≥ 100/200 as "at least this many," not precise.
