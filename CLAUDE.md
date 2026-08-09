# Israel Jobs Dashboard — Project Context

## What this is
A scraper + Chart.js dashboard tracking open tech jobs on **devjobs.co.il** (Israeli job board, ~3,100 listings)
plus the tech-classified subset of **lin-srael.com** ("LinSrael Insights" — a Base44-built site that pulls
Israel job postings straight from LinkedIn's public search, ~1,800 tech-relevant listings out of its ~12,000
total across all professions). Daily snapshots go to SQLite; a static `data.js` feeds the `index.html`
dashboard. No server needed — open `index.html` directly.

Both sources' job IDs are LinkedIn job IDs (devjobs.co.il sources from LinkedIn directly; lin-srael's
`linkedin_job_id` is the same ID space), so jobs seen by both get merged into one `job_index` row tagged
`source='both'` instead of duplicated. See `upsert_job_index()` in `jobs_tracker.py` for the merge logic and
`fetch_jobs_linsrael()` for the scraper (calls lin-srael's public `searchJobs` function API directly — no
HTML scraping needed there, unlike devjobs.co.il).

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
py jobs_tracker.py                   # full scrape (devjobs.co.il + lin-srael) + export (takes ~3-4 min)
py jobs_tracker.py export            # re-export data.js from existing DB (no scrape)
py jobs_tracker.py company <Name>    # targeted single-company scrape (fast test, devjobs.co.il only)
py jobs_tracker.py linsrael          # standalone lin-srael.com scrape — upserts/merges into job_index; marks
                                      #   removals only for source='linsrael'-only rows (safe — lin-srael's own
                                      #   history is complete for those); leaves 'devjobs'/'both' rows untouched
py jobs_tracker.py status            # print snapshot summary table
py jobs_tracker.py loop              # run every 24 h (blocking)
py jobs_tracker.py linkedin [N]      # manual: enrich N active jobs (default 15) with LinkedIn posted-time/applicants, no filters
py jobs_tracker.py linkedin-data     # automated daily policy: Data/ML jobs only, first_seen >= launch date, capped at 100 applicants, max 99/day
```

**Windows Task Scheduler** runs the full scrape daily automatically.
**GitHub Actions** (`.github/workflows/daily-scrape.yml`) also runs daily at 06:00 UTC and commits both `data.js` and `jobs.db` back to the repo. `jobs.db` is tracked in git (not in .gitignore) so historical snapshots are never lost if the runner restarts.

The plain (no-args) run and the GitHub Actions workflow both call `run_once()`, which scrapes **both** sources
and marks a job removed only if it's absent from the union of everything scraped that session — a job absent
from devjobs.co.il today but still seen on lin-srael (or vice versa) correctly stays active.

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
              linkedin_applicant_n, linkedin_checked,
              source)
             -- one canonical row per unique job
             -- date_removed = '' means still active
             -- source of truth for the dashboard's raw table
             -- job_id doubles as the LinkedIn job ID (devjobs.co.il sources from LinkedIn,
             --   so https://www.linkedin.com/jobs/view/{job_id}/ resolves directly; lin-srael's
             --   linkedin_job_id is the same ID space)
             -- linkedin_* columns populated by `linkedin` / `linkedin-data` commands only
             -- source: 'devjobs' | 'linsrael' | 'both' — which scraper(s) have seen this job_id.
             --   'devjobs' is treated as authoritative for company/title/url/dev_type/work_mode/
             --   location/posted_date (devjobs.co.il's structured cards parse more precisely than
             --   lin-srael's free-text listing); a 'linsrael' upsert only fills those fields in
             --   when devjobs never had the row, so it can never clobber devjobs' data. See
             --   upsert_job_index() in jobs_tracker.py.

repost_events (id, job_id, company, title,
               closed_date, reopened_date, gap_days)
             -- one row per close->reopen cycle for a job_id (same LinkedIn job ID marked
             --   date_removed, then seen again in a later scrape). Logged inside
             --   upsert_job_index() at the moment it detects the reopen, BEFORE the upsert
             --   clears job_index.date_removed — otherwise that history is unrecoverable
             --   (job_index only ever stores current state, one date_removed per job_id).
             -- gap_days = reopened_date - closed_date; captures anywhere from same-day
             --   reposts to multi-year gaps.
             -- Does NOT catch a company reposting the same role under a brand-new job_id
             --   (common on LinkedIn) — that would need fuzzy company/title matching across
             --   job_ids, which was deliberately left out (noisy for generic titles).
```

### job_index lifecycle
- **Full scrape** (`run_once`): upserts today's jobs from BOTH sources; marks a job removed only if it's
  absent from the union of everything scraped that session (see below)
- **Company scrape** (`run_company`): upserts that company's jobs only (devjobs.co.il); never marks removals
- **Standalone lin-srael scrape** (`linsrael` command): upserts/merges lin-srael's jobs; marks removals for
  source='linsrael'-only rows (jobs we've only ever seen via lin-srael — its own scrape history is a complete
  record for them, so absence today is a real signal). 'devjobs'/'both' rows are left untouched since
  lin-srael's tech subset doesn't cover devjobs' full universe — safe to mark those as removed only when both
  sources ran together in the same session, which `run_once()` does (union of both sources' today-ids)
- **Re-listed job**: `date_removed` cleared back to `''`, `first_seen` preserved
- **Bootstrap**: on first run after adding `job_index`, auto-populated from `job_records` history (tagged `source='devjobs'`)

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
    "isActive",      // boolean
    "source",        // 'devjobs' | 'linsrael' | 'both'
    "repostCount",   // number of close->reopen cycles logged for this job_id, from repost_events
    "repostHistory"  // [{closedDate, reopenedDate, gapDays}, ...] — one entry per cycle
  }, ...]
}
```

---

## Dashboard — index.html features

### Data source toggle
A two-button pill in the header ("devjobs.co.il only" / "+ lin-srael (LinkedIn)") lets you switch between
devjobs.co.il-only and combined data. Preference is stored in `localStorage` (`linsrael-include`, default:
include) and applied by filtering `D.jobRecords` right after load, then reloading the page on toggle — this
is deliberate: the whole dashboard (stats bar, all charts, ranking table, raw table) is already
`D.jobRecords`-driven client-side (nothing depends on the server-precomputed `D.datasets`/`D.allRanking`
fields, which are legacy/unused by the current UI), so filtering the source array once at the top of the
script before anything else runs is simpler and safer than trying to live-patch state captured in already-
built chart closures.

### Stats bar
Jobs in snapshot · Companies · Days tracked · Latest snapshot date

### Filter bar 1 — Company chips
Top-30 company chips (colored) to toggle chart lines. Buttons: All / None / Top 10.

### Filter bar 2 — Metadata filters
- **Mode** dropdown (workMode: Hybrid / On-site / Remote — empty for lin-srael-sourced jobs, see known issues)
- **Location** dropdown (location: city names)
- **Title** text input (keyword search on title only)
- **Listed** dropdown (filter by `firstSeen` date)
- **Status** dropdown — "Active now" shows only jobs with `dateRemoved = ''`; specific dates show jobs removed on that day
- **Raw search** box (matches company OR title)
- Role/category filtering is via the **Label chips** above the role-trend chart (client-side `LABEL_DEFS`
  regex classification on title — see below), not a `devType`-backed dropdown; the exported `devType` field
  is legacy and currently unused by the frontend.

All filters combine with AND logic.

### Line chart
The main "Open positions over time" chart is a stacked area chart by posting age band (Older than 90 days /
30–90 / 7–30 / New last 7 days), computed **client-side** from `D.jobRecords` (`buildTotalDataset()`) — not
from the server-precomputed `datasets`/`labels` fields, which are legacy/unused (still generated by
`export_data_js` for backward compatibility, harmless but dead).

### Role/mode/district trend chart
`LABEL_DEFS` (title regex → role category, includes an `'Other'` catch-all so every job always gets ≥1 label)
drives the "Labels" raw-table column, the role breakdown chart, the group chips, and the applicant-demand
chart. Toggle between Label / Mode / District breakdowns with the buttons above that chart.

### Ranking table
All companies ranked by current active job count — computed **client-side** from `D.jobRecords`
(`renderRanking()`), not from the server-precomputed `allRanking` field (same legacy/unused status as
`datasets` above) — scrollable, max-height 420 px.

### Raw data table
One row per unique job (from `job_index`). Columns: First Seen / Company / Job Title / Role (badge) / Mode / Location / Days / Removed / Reposts / LI Posted / Applicants / Link.
- **Days** — how long the job was/has been listed (tooltip shows exact first/last seen dates)
- **Removed** — green "active" or red removal date
- **Reposts** — amber "N×" badge if the job's LinkedIn ID was ever closed and later reopened (from `repost_events`/`repostHistory`); hover for the closed→reopened date and gap-days of each cycle. "—" if never reposted. Only catches same-job_id reposts (see known issue 9).
Sortable by any column. Shows filtered count vs total.

---

## Pending / known issues

1. **work_mode and location empty for 2026-05-23 records** — can't be backfilled (data not captured).
2. **Intel has no current listings** on devjobs.co.il (verified 2026-05-24).
3. **TOP_N = 30** — line chart only shows top 30 companies. Ranking table shows all.
4. **LinkedIn enrichment (`linkedin-data`)** covers all jobs (any `dev_type`) first seen exactly `LINKEDIN_OFFSET_DAYS` (2) days ago. Polling for a job stops once its applicant count reaches 100 (`LINKEDIN_APPLICANT_CAP`). Capped at `LINKEDIN_DAILY_LIMIT` (150) requests/run. Runs automatically every day as a step in the daily GitHub Actions workflow (`.github/workflows/daily-scrape.yml`), right after the main scrape.
5. **LinkedIn applicant count is the older "applicants" widget, not what you see logged in.** Our scraper hits the public/logged-out HTML, which only exposes `.num-applicants__caption` (e.g. "Over 200 applicants"). LinkedIn's logged-in UI now shows a different, separate metric — "X people clicked apply" — especially for jobs with "Responses managed off LinkedIn" (external ATS), where LinkedIn can't count real applicants and only counts Apply-button clicks. That text isn't present in the anonymous HTML at all, so the dashboard's number can be stale or simply a different metric than what you see browsing LinkedIn directly. Also note: "Over N applicants"/"Over 200 applicants" is a floor, not exact — `parse_applicant_count()` just extracts the first number, so treat values ≥ 100/200 as "at least this many," not precise.
6. **lin-srael-sourced jobs always have `workMode = ''`** — lin-srael's `searchJobs` API doesn't return a remote/hybrid/on-site field in results (only accepts it as an unconfirmed filter param), so the Mode filter/column is blank for `source: 'linsrael'` rows. Not backfillable without finding that field.
7. **Company filter chips (top-30) are still devjobs.co.il-only** — `topCompanies`/`ALL_COMPANIES` is computed from the `snapshots` table (devjobs peak counts only); lin-srael-only companies won't get a quick-filter chip. Use the raw-search text box (matches company name) to filter by them instead.
8. **Standalone `linsrael` command marks removals only for `source='linsrael'`-only rows** (by design — see job_index lifecycle above). A job that's also seen by devjobs (`source='devjobs'`/`'both'`) only gets its `isActive` status properly maintained through the daily `run_once()` combined-source flow, not through ad-hoc `linsrael` runs.
9. **Repost tracking (`repost_events`) only started 2026-08-09** — any close→reopen cycles before that date are invisible; `job_index.date_removed` was silently overwritten on relist with no history kept. It also only catches a job reappearing under the *same* LinkedIn job ID. A company reposting the same role under a brand-new job_id (common on LinkedIn) looks like an unrelated new listing — no fuzzy company/title matching was added to catch that (deliberately skipped, see conversation: too noisy for generic titles).
