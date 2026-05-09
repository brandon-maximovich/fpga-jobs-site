# FPGA Remote Jobs — Live Feed

Auto-aggregated FPGA remote job postings (no clearance required).
Live site: <https://brandon-maximovich.github.io/fpga-jobs/>

## How it works

1. `fetch_fpga_jobs.py` hits public APIs from Remotive, Remote OK, Hacker News, Greenhouse, Lever, and Ashby.
2. Filters for FPGA roles that allow remote and exclude clearance/ITAR requirements.
3. Persists URLs to `fpga_jobs.json` with `first_seen` / `last_seen` timestamps.
4. Regenerates `index.html` showing:
   - **New in last 24 hours** (since last script run)
   - **Still open from earlier**
   - **No longer posted** (dropped from feeds in last 7 days)
5. GitHub Actions (`.github/workflows/update-jobs.yml`) runs the script daily at 06:00 UTC and commits the updated HTML.

## Local use

```bash
python fetch_fpga_jobs.py
# then open index.html in a browser
```

## Manual trigger (live site)

Go to the [Actions tab](https://github.com/brandon-maximovich/fpga-jobs/actions),
select **Update FPGA Jobs**, click **Run workflow**.

## Adding more company sources

Edit the lists in `fetch_fpga_jobs.py`:
- `GREENHOUSE_ORGS` — slug from `boards.greenhouse.io/<slug>`
- `LEVER_ORGS` — slug from `jobs.lever.co/<slug>`
- `ASHBY_ORGS` — slug from `jobs.ashbyhq.com/<slug>`
