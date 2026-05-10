# Continuous Improvement Guide

How to iterate on the FPGA Jobs site safely, without breaking the live deployment.

---

## 0. The basic loop

Every change follows the same four steps:

```bash
cd C:\Users\charl\fpga-jobs-site

# 1. Pull whatever the cron committed since you last touched the repo
git pull

# 2. Edit files in your editor

# 3. Test locally before pushing
py fetch_fpga_jobs.py
# open index.html in a browser and verify it looks right

# 4. Commit and push
git add -A
git commit -m "Short description of the change"
git push
```

Pushing to `main` triggers the workflow's `push` path (it reruns the script and republishes), so your change appears live in ~1-2 minutes.

> **Always run `git pull` first.** The cron commits to `main` once a day. If you forget to pull and try to push, git will reject your push with a non-fast-forward error.

---

## 1. Recipe: Add a new Greenhouse company

**When to use:** You found a company hiring FPGA roles and their job board URL is `https://boards.greenhouse.io/<slug>` or `https://job-boards.greenhouse.io/<slug>`.

**Step 1.** Find the slug. Visit the company's careers page and look for the URL pattern. Example: `https://boards.greenhouse.io/wayve` → slug is `wayve`.

**Step 2.** Verify the API returns FPGA jobs (do this BEFORE editing the code):

```bash
curl -s "https://boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true" | py -c "import sys,json; d=json.load(sys.stdin); titles=[j['title'] for j in d.get('jobs',[]) if 'fpga' in (j.get('title','') + j.get('content','')).lower()]; print('\n'.join(titles) or 'NO FPGA ROLES FOUND')"
```

If you see role titles, the slug works and there are FPGA roles. If you see `NO FPGA ROLES FOUND`, the slug works but there's nothing to scrape today — still worth adding for future runs.

**Step 3.** Edit `fetch_fpga_jobs.py`:

```python
GREENHOUSE_ORGS = [
    "drweng", "virtu", "wehrtyou", "wayve", "tanius", "aeyeinc",
    "imc",
    "your-new-slug-here",   # <-- add it
]
```

**Step 4.** Test, commit, push:

```bash
py fetch_fpga_jobs.py   # verify no new errors, look at output
git add fetch_fpga_jobs.py
git commit -m "Add <company> to Greenhouse sources"
git push
```

---

## 2. Recipe: Add a new Lever company

Same pattern, different API:

```bash
# Verify API works:
curl -s "https://api.lever.co/v0/postings/<slug>?mode=json" | py -c "import sys,json; d=json.load(sys.stdin); titles=[j['text'] for j in d if 'fpga' in (j.get('text','') + j.get('descriptionPlain','')).lower()]; print('\n'.join(titles) or 'NO FPGA ROLES')"
```

Then add to `LEVER_ORGS` in the script.

> Lever slugs are sometimes weird (`mythic-ai.com`, `arraylabs.io`). Always verify with a curl first — don't guess.

---

## 2.5. Recipe: Add a new Workday tenant (AMD/NVIDIA/Intel/etc.)

Workday hosts the careers pages of most large hardware companies. **Use `probe_workday.py` first** — Workday's API has quirks (requires `Referer` header, max `limit=20`).

**Step 1.** Find the tenant + cluster + site from a public job URL. Example:
`https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/...`
→ tenant=`nvidia`, cluster=`wd5`, site=`NVIDIAExternalCareerSite`.

**Step 2.** Add to the candidate list at the top of `probe_workday.py` and run:

```bash
py probe_workday.py
```

A successful tenant returns `http=200, total_fpga > 0`. Common error codes:
- `400` — body or headers wrong (see top of `fetch_workday()` for the exact format)
- `422` — site path is wrong
- `404` — tenant doesn't exist or moved

**Step 3.** Add the working tuple to `WORKDAY_BOARDS` in `fetch_fpga_jobs.py`:

```python
WORKDAY_BOARDS = [
    ("nvidia", "wd5", "NVIDIAExternalCareerSite"),
    ("your-tenant", "wd1", "External_Career"),  # <-- add it
]
```

**Step 4.** Test, commit, push:

```bash
py fetch_fpga_jobs.py
git add -A
git commit -m "Add <company> Workday tenant"
git push
```

> **Filter behavior:** Workday listings don't include the JD, so we use a two-tier title filter. **Strong matches** (FPGA/RTL/Verilog/VHDL in title) are always included. **Medium matches** (broader "verification engineer", "hardware design") are included only when location is explicitly Remote/Virtual.

---

## 3. Recipe: Add a new Ashby company

```bash
curl -s "https://api.ashbyhq.com/posting-api/job-board/<slug>" | py -c "import sys,json; d=json.load(sys.stdin); titles=[j['title'] for j in d.get('jobs',[]) if 'fpga' in (j.get('title','') + j.get('descriptionPlain','')).lower()]; print('\n'.join(titles) or 'NO FPGA ROLES')"
```

Then add to `ASHBY_ORGS`.

---

## 4. Recipe: Fix a 404 on an existing slug

When the cron logs show `WARN: Greenhouse foo failed: HTTP Error 404`, the company has either renamed their board, moved to a different ATS, or shut it down.

**Diagnose:**

```bash
# Confirm the 404
curl -I "https://boards-api.greenhouse.io/v1/boards/foo/jobs"

# Check if they moved to another ATS by Googling: "<company> careers"
# Look for boards.greenhouse.io vs jobs.lever.co vs jobs.ashbyhq.com vs careers.<company>.com
```

**Fix:**
- If they renamed: update the slug in the right list.
- If they moved ATS: remove from the old list, add to the new one.
- If they shut down: remove from the list entirely.

---

## 5. Recipe: Add an entirely new source

You found a job board with a public API (e.g., a niche FPGA-specific board).

**Pattern:** copy one of the existing fetchers and adapt. For example, `fetch_remotive()`:

```python
def fetch_NEW_SOURCE() -> list[dict]:
    out = []
    try:
        data = fetch_json("https://example.com/api/jobs?q=fpga")
        for j in data.get("results", []):
            title = j.get("name", "")
            desc = j.get("body", "") or ""
            if not is_fpga(title, desc):
                continue
            if not is_remote(j.get("loc", ""), desc):
                continue
            out.append({
                "url": j["apply_url"],
                "title": title,
                "company": j.get("employer", ""),
                "location": j.get("loc", "Remote"),
                "source": "NewSource",
                "posted_at": j.get("created", ""),
            })
    except Exception as e:
        logging.warning("NewSource failed: %s", e)
    return out
```

Then call it from `fetch_all()`:

```python
print("[7/7] NewSource...", flush=True)
n0 = len(jobs); jobs += fetch_NEW_SOURCE()
print(f"  +{len(jobs) - n0} (total {len(jobs)})")
```

**Required fields per job:** `url`, `title`, `company`, `location`, `source`, `posted_at`. Missing fields will render as empty strings — not fatal, but ugly.

---

## 6. Recipe: Fix HN false positives

The HN fetcher currently catches comments where someone says "I'm available remotely for FPGA work" instead of actual job postings. To filter only real "Who is Hiring" job postings:

In `fetch_hn()`, add a stricter pattern check. HN job comments use `|` separators:

```python
# Inside fetch_hn(), after the is_fpga check:
HIRING_RE = re.compile(r"\|\s*(remote|hiring)\b|\bwe(\'re|\s+are)\s+hiring\b|\blooking\s+for\b", re.I)
if not HIRING_RE.search(text):
    continue
```

Or filter to only "Ask HN: Who is hiring" parent threads:

```python
# Restrict to comments under specific parent stories
parent_id = h.get("story_id")
# Only keep comments under official "Who is Hiring" threads (story_id known per month)
```

Test with `py fetch_fpga_jobs.py` and check the HN entries in `fpga_jobs.json` afterward.

---

## 7. Recipe: Change the cron schedule

Edit `.github/workflows/update-jobs.yml`:

```yaml
on:
  schedule:
    - cron: '0 6 * * *'   # daily 06:00 UTC
    - cron: '0 18 * * *'  # also daily 18:00 UTC -- run twice/day
```

[crontab.guru](https://crontab.guru/) is the right tool for picking cron expressions.

> GitHub Actions delays scheduled runs by 5-30 min during peak load. For tight timing, trigger manually.

---

## 8. Recipe: Trigger an immediate refresh

**Option A — via GitHub UI:**
1. Go to <https://github.com/brandon-maximovich/fpga-jobs-site/actions>
2. Click **Update FPGA Jobs** → **Run workflow** → **Run workflow**

**Option B — via push:** any commit to `main` triggers it (because the workflow has `push: branches: [main]`).

---

## 9. Recipe: Roll back a bad change

If you push something that breaks the site:

```bash
# See recent commits
git log --oneline -10

# Revert the bad commit (creates a new commit that undoes it)
git revert <bad-commit-sha>
git push
```

`git revert` is safer than `git reset --hard` because it doesn't rewrite history — and if the cron has already committed since your bad push, reset would lose those changes.

---

## 10. Recipe: Use a branch for risky changes

For anything more than a one-line change, work on a branch:

```bash
git checkout -b add-new-source
# edit, test, commit
git push -u origin add-new-source
```

Then on GitHub, open a pull request from `add-new-source` → `main`. Merging the PR triggers the workflow. If something breaks, just revert the merge commit instead of fighting with reset.

---

## 11. Monitoring: where to look when things break

| Symptom | Where to look |
|---|---|
| Site shows old data | <https://github.com/brandon-maximovich/fpga-jobs-site/actions> — check if last run failed |
| Workflow shows red ❌ | Click the failed run → expand the failed step for the error log |
| One source returns 0 every day | The org slug 404'd, or the API changed shape — see Recipe #4 |
| `git push` rejected | Run `git pull --rebase` first |
| Pages URL gives 404 | Settings → Pages → confirm Source is `main` branch, root folder |
| Cron isn't firing | Pages must be public; private repos lose Actions cron after 60 days of inactivity |

---

## 11.3. Recipe: Adjust the freshness window

The script ships with a strict **24-hour** age filter. Anything posted more than 24 hours before the run is excluded. This keeps the feed truly fresh but also means you'll often see 0-3 ATS results per day (companies don't post FPGA roles daily).

**To relax the window**, set the `MAX_AGE_HOURS` env var. Common values:

| Value | Meaning | Typical daily volume |
|---|---|---|
| `24` (default) | Past day only | 0–10 jobs |
| `72` | Past 3 days | 5–25 jobs |
| `168` | Past week | 15–60 jobs |
| `720` | Past month | 40–150 jobs |

**To override on GitHub Actions**, edit `.github/workflows/update-jobs.yml`:

```yaml
- name: Run aggregator
  env:
    MAX_AGE_HOURS: '168'   # past week
    SERPAPI_KEY: ${{ secrets.SERPAPI_KEY }}
    RAPIDAPI_KEY: ${{ secrets.RAPIDAPI_KEY }}
  run: python fetch_fpga_jobs.py
```

**To override locally:**
```bash
MAX_AGE_HOURS=168 py fetch_fpga_jobs.py
```

> **Why strict-by-default?** The existing UI already tracks `first_seen` per URL, so older roles you've already reviewed don't repeat. Combined with the 24h filter, every job you see in "New today" is genuinely fresh — no scrolling past stale postings.

---

## 11.4. Recipe: Enable LinkedIn/Indeed coverage via JSearch (RapidAPI)

JSearch wraps LinkedIn, Indeed, Glassdoor, and ZipRecruiter behind one ToS-compliant API. **Biggest single source of new jobs** — likely +20–80 remote FPGA roles per run.

**Free tier:** ~200 queries/month. The script uses 1 query/run, so daily cron uses ~30/month.

**Step 1 — subscribe (you already signed up):**
1. Go to <https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch>
2. Click **Subscribe to Test** → choose the **Basic (Free)** plan.
3. Copy your `X-RapidAPI-Key` from the Endpoints page.

**Step 2 — add it as a GitHub Actions secret:**
1. Go to `https://github.com/brandon-maximovich/fpga-jobs-site/settings/secrets/actions`
2. Click **New repository secret**.
3. Name: `RAPIDAPI_KEY`. Value: your key. Save.

The next workflow run will auto-pick it up. Look for `[N/N] JSearch (LinkedIn/Indeed/Glassdoor via RapidAPI)` in the run logs.

**To use locally:**
```bash
# PowerShell
$env:RAPIDAPI_KEY = "your-key-here"
py fetch_fpga_jobs.py

# Git Bash
RAPIDAPI_KEY=your-key-here py fetch_fpga_jobs.py
```

**Tweaking queries:** edit `JSEARCH_QUERIES` in `fetch_fpga_jobs.py`. Each query consumes one of your monthly credits. Multiple queries hit different result pools; a smart set might be:
```python
JSEARCH_QUERIES = [
    ("FPGA engineer", "week"),
    ("FPGA verification", "week"),
    ("RTL design engineer remote", "week"),
]
```
Three queries × 30 days = 90/month, still inside free tier.

---

## 11.5. Recipe: Enable broad-web search via SerpAPI (free tier)

The script supports an optional 7th fetcher that hits Google via [SerpAPI](https://serpapi.com/) to find FPGA-remote jobs across the entire indexed web — beyond the ATS APIs.

**Free tier:** 100 searches/month. The script uses 3 queries/run, so daily cron uses ~90/month.

**Step 1 — get an API key:**
1. Sign up at <https://serpapi.com/users/sign_up> (free, no credit card).
2. Copy your API key from the dashboard.

**Step 2 — add it as a GitHub Actions secret:**
1. Go to `https://github.com/brandon-maximovich/fpga-jobs-site/settings/secrets/actions`
2. Click **New repository secret**.
3. Name: `SERPAPI_KEY`. Value: your API key. Save.

The next workflow run will automatically pick it up. The 7th step `[7/7] SerpAPI broad-web search` will appear in the run logs.

**To use locally:**
```bash
# PowerShell
$env:SERPAPI_KEY = "your-key-here"
py fetch_fpga_jobs.py

# Git Bash
SERPAPI_KEY=your-key-here py fetch_fpga_jobs.py
```

**To disable:** delete the secret on GitHub or unset the env var locally. The fetcher silently no-ops without it.

**Tweaking the queries:** edit the `SERPAPI_QUERIES` list in `fetch_fpga_jobs.py`. Each query consumes one of your 100 monthly credits.

---

## 12. Suggested next improvements

In order of impact-vs-effort:

1. **Tighten HN filter** (Recipe #6) — biggest current source of noise.
2. **Add 5–10 more company sources** — most companies in the dashboard's list (`fpga-remote-jobs.html`) probably have working ATS slugs you can add. Each one adds ~0–3 jobs to the feed.
3. **Add a Slack/Discord/email notifier** — extend the workflow to post new URLs to a webhook when the day's diff is non-empty. Skeleton:
   ```yaml
   - name: Notify
     if: success()
     run: |
       NEW=$(jq -r '.jobs | to_entries | map(select(.value.first_seen >= (now - 86400 | todate))) | length' fpga_jobs.json)
       if [ "$NEW" -gt 0 ]; then
         curl -X POST -H 'Content-type: application/json' \
           --data "{\"text\":\"$NEW new FPGA remote jobs today\"}" \
           ${{ secrets.SLACK_WEBHOOK }}
       fi
   ```
   Store the webhook URL in repo Settings → Secrets and variables → Actions.
4. **Add a "salary" column** when sources expose it (Greenhouse, Ashby do; Lever sometimes).
5. **Filter by region in the UI** (radio buttons that hide rows by location text).
6. **RSS feed output** — generate `feed.xml` alongside `index.html` so you can subscribe in any RSS reader.

---

## 13. Local-only experiments without polluting the live site

If you want to test something without touching `main`:

```bash
git checkout -b experiment
py fetch_fpga_jobs.py
# look at index.html, iterate
# if you decide it's not worth shipping:
git checkout main
git branch -D experiment
```

You can even point the script at a different output file locally for side-by-side comparison:

```python
HTML_PATH = ROOT / "experiment.html"  # temporary - revert before commit
```
