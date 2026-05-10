"""FPGA Remote Jobs aggregator.

Fetches FPGA remote job postings from public APIs, dedupes, persists to JSON,
and regenerates a static HTML dashboard.

Sources:
  - Remotive (public API)
  - Remote OK (public API)
  - Hacker News "Who is Hiring" comments (Algolia API)
  - Greenhouse boards (public API) for known FPGA employers
  - Lever boards (public API)
  - Ashby boards (public API)

Run daily:
    python fetch_fpga_jobs.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / "fpga_jobs.json"
HTML_PATH = ROOT / "index.html"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 FPGAJobAggregator/1.0"
TIMEOUT = 30

# Max age of a posting in hours. Default 24 = only show jobs posted in the
# last day. Override via env var, e.g. MAX_AGE_HOURS=168 for a 1-week window.
MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS", "24"))

FPGA_STRICT_RE = re.compile(r"\bfpga\b", re.I)
EXCLUDE_RE = re.compile(
    r"\bsecurity clearance\b|\bsecret clearance\b|\btop secret\b|\bts/sci\b|"
    r"\bus citizen(ship)?\b|\bclearance required\b|\bactive clearance\b|\bitar\b",
    re.I,
)

GREENHOUSE_ORGS = [
    # HFT
    "drweng", "virtu", "wehrtyou", "imc", "optiverus",
    "janestreet", "jumptrading",
    # AI accelerators / quantum
    "tenstorrent", "lightmatter", "psiquantum", "ionq",
    # Autonomy
    "wayve", "nuro", "aurorainnovation", "waymo",
    # Sensors / hardware
    "tanius", "aeyeinc", "vast",
]
LEVER_ORGS = [
    "loftorbital", "kapta-space", "arraylabs.io", "reliable",
    "CesiumAstro", "waabi",
]
ASHBY_ORGS = [
    "keyrock", "radiant-industries", "d-matrix", "saronic",
    "rain", "perplexity",
]

# Workday tenants. Tuple format: (tenant, cluster, site_path).
# Verify new entries with probe_workday.py before adding.
WORKDAY_BOARDS = [
    ("nvidia", "wd5", "NVIDIAExternalCareerSite"),
    ("intel", "wd1", "External"),
    ("cadence", "wd1", "External_Careers"),
    ("marvell", "wd1", "MarvellCareers2"),
    ("broadcom", "wd1", "External_Career"),
    ("hpe", "wd5", "Jobsathpe"),
    ("micron", "wd1", "External"),
    ("globalfoundries", "wd1", "External"),
]


def fetch_url(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def fetch_json(url: str, headers: dict | None = None):
    return json.loads(fetch_url(url, headers).decode("utf-8", errors="replace"))


def is_fpga(title: str, content: str = "") -> bool:
    text = f"{title} {content}".lower()
    if EXCLUDE_RE.search(text):
        return False
    return bool(FPGA_STRICT_RE.search(text))


# Stricter: FPGA must appear in the title (or closely related hardware role title).
# Used for ATS sources where the description is a long full JD that often
# mentions FPGA only incidentally as a "nice to have" skill.
TITLE_FPGA_RE = re.compile(
    r"\bfpga\b|\brtl\b|\bsystem\s*verilog\b|\bdigital design\b|"
    r"\bhardware design\b|\bverification engineer\b|\bdv engineer\b|"
    r"\bfield.programmable gate array",
    re.I,
)


def is_fpga_role_strict(title: str, content: str = "") -> bool:
    """Require FPGA-ish keywords in the TITLE, not just the description."""
    if EXCLUDE_RE.search(f"{title} {content}".lower()):
        return False
    if not TITLE_FPGA_RE.search(title):
        return False
    # Belt-and-braces: also require the JD to mention FPGA somewhere
    return bool(FPGA_STRICT_RE.search(f"{title} {content}"))


WORKDAY_TITLE_RE = re.compile(
    r"\bfpga\b|\brtl\b|\bsystem\s*verilog\b|\bverilog\b|\bvhdl\b|"
    r"\bfield.programmable gate array",
    re.I,
)


def is_fpga_title_only(title: str) -> bool:
    """Title-only filter for sources without descriptions in listings (Workday).

    Stricter than is_fpga_role_strict: must literally mention FPGA/RTL/Verilog.
    "Verification Engineer" alone is NOT enough -- at semi companies on Workday,
    most verification roles are ASIC, not FPGA.
    """
    if EXCLUDE_RE.search(title.lower()):
        return False
    return bool(WORKDAY_TITLE_RE.search(title))


def is_remote(location: str, content: str = "") -> bool:
    text = f"{location} {content}".lower()
    return any(k in text for k in
               ("remote", "anywhere", "work from home", "wfh", "distributed", "virtual"))


def parse_posted_age_hours(posted_at, source: str = "") -> float | None:
    """Return hours since posted, or None if unparseable.

    Handles:
    - ISO 8601 strings ("2026-05-08T12:00:00Z", with or without TZ)
    - Numeric epoch (seconds or milliseconds)
    - Workday-style human text ("Posted Today", "Yesterday", "30+ Days Ago")
    """
    if posted_at is None or posted_at == "":
        return None
    s = str(posted_at).strip()
    low = s.lower()

    # Workday-style human text
    if any(w in low for w in ("today", "yesterday", "ago")):
        if "today" in low:
            return 0.0
        if "yesterday" in low:
            return 24.0
        m = re.search(r"(\d+)\s*\+?\s*hour", low)
        if m:
            return float(m.group(1))
        m = re.search(r"(\d+)\s*\+?\s*day", low)
        if m:
            return float(m.group(1)) * 24
        m = re.search(r"(\d+)\s*\+?\s*week", low)
        if m:
            return float(m.group(1)) * 24 * 7
        m = re.search(r"(\d+)\s*\+?\s*month", low)
        if m:
            return float(m.group(1)) * 24 * 30
        return None

    # Numeric epoch
    if s.lstrip("-").isdigit():
        ts = int(s)
        if ts > 1e12:
            ts /= 1000.0  # ms -> s
        delta = datetime.now(timezone.utc).timestamp() - ts
        return delta / 3600

    # ISO 8601
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - dt).total_seconds()
        return delta / 3600
    except Exception:
        return None


def is_recent(posted_at, source: str = "") -> bool:
    """Strict: include only if we can confirm posted_at is within MAX_AGE_HOURS.
    Unparseable timestamps are EXCLUDED (we cannot confirm freshness)."""
    age = parse_posted_age_hours(posted_at, source)
    return age is not None and 0 <= age <= MAX_AGE_HOURS


def fetch_remotive() -> list[dict]:
    out = []
    try:
        data = fetch_json("https://remotive.com/api/remote-jobs?search=fpga")
        for j in data.get("jobs", []):
            title = j.get("title", "")
            desc = j.get("description", "") or ""
            if not is_fpga(title, desc):
                continue
            if not is_recent(j.get("publication_date", ""), "Remotive"):
                continue
            out.append({
                "url": j.get("url", ""),
                "title": title,
                "company": j.get("company_name", ""),
                "location": j.get("candidate_required_location", "Remote"),
                "source": "Remotive",
                "posted_at": j.get("publication_date", ""),
            })
    except Exception as e:
        logging.warning("Remotive failed: %s", e)
    return out


def fetch_remoteok() -> list[dict]:
    out = []
    try:
        data = fetch_json("https://remoteok.com/api")
        if not isinstance(data, list):
            return out
        for j in data:
            if not isinstance(j, dict):
                continue
            title = j.get("position", "") or ""
            tags = j.get("tags", []) or []
            tags_str = " ".join(tags) if isinstance(tags, list) else ""
            desc = j.get("description", "") or ""
            if not is_fpga(f"{title} {tags_str}", desc):
                continue
            posted = j.get("date") or j.get("epoch", "")
            if not is_recent(posted, "Remote OK"):
                continue
            url = j.get("url") or j.get("apply_url", "")
            if not url:
                continue
            if url.startswith("/"):
                url = "https://remoteok.com" + url
            out.append({
                "url": url,
                "title": title,
                "company": j.get("company", ""),
                "location": j.get("location", "Remote"),
                "source": "Remote OK",
                "posted_at": str(posted),
            })
    except Exception as e:
        logging.warning("Remote OK failed: %s", e)
    return out


_HN_HIRING_RE = re.compile(
    r"\b(we\s*(\'re|are)\s*hiring|hiring\b|seeking|looking\s+for|"
    r"join\s+(us|our|the\s+team)|come\s+work|\|\s*remote)",
    re.I,
)
_HN_SEEKER_RE = re.compile(
    r"\bseeking\s+(work|employment|a\s+job|opportunit|role|position)\b|"
    r"\b(am|i\'m|i\s+am)\s+(available|looking|seeking)\s+for\s+(remote|work)|"
    r"\bopen\s+to\s+(work|new\s+opportunit)|\bavailable\s+for\s+(hire|work|"
    r"contract|consulting)|\bSEEKING\s+(WORK|FREELANCE)",
    re.I,
)


def fetch_hn() -> list[dict]:
    out = []
    try:
        epoch_7d = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
        url = (
            "https://hn.algolia.com/api/v1/search_by_date"
            "?query=FPGA&tags=comment"
            f"&numericFilters=created_at_i%3E{epoch_7d}"
            "&hitsPerPage=50"
        )
        data = fetch_json(url)
        for h in data.get("hits", []):
            text = h.get("comment_text") or ""
            if not text or not is_fpga(text) or not is_remote("", text):
                continue
            # Skip "I'm available for remote work" job-seeker comments
            if _HN_SEEKER_RE.search(text):
                continue
            # Require at least one "we're hiring" signal
            if not _HN_HIRING_RE.search(text):
                continue
            if not is_recent(h.get("created_at", ""), "Hacker News"):
                continue
            obj_id = h.get("objectID")
            if not obj_id:
                continue
            snippet = re.sub(r"<[^>]+>", " ", text)
            snippet = re.sub(r"\s+", " ", snippet).strip()
            out.append({
                "url": f"https://news.ycombinator.com/item?id={obj_id}",
                "title": (snippet[:140] + "...") if len(snippet) > 140 else snippet,
                "company": "(via HN Who is Hiring)",
                "location": "Remote (see comment)",
                "source": "Hacker News",
                "posted_at": h.get("created_at", ""),
            })
    except Exception as e:
        logging.warning("HN failed: %s", e)
    return out


def fetch_greenhouse(org: str) -> list[dict]:
    out = []
    try:
        url = f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs?content=true"
        data = fetch_json(url)
        for j in data.get("jobs", []):
            title = j.get("title", "")
            content = j.get("content", "") or ""
            location = (j.get("location") or {}).get("name", "")
            if not is_fpga_role_strict(title, content):
                continue
            if not is_remote(location, content):
                continue
            posted = j.get("updated_at", "") or j.get("first_published", "")
            if not is_recent(posted, "Greenhouse"):
                continue
            out.append({
                "url": j.get("absolute_url", ""),
                "title": title,
                "company": org,
                "location": location or "Remote",
                "source": f"Greenhouse:{org}",
                "posted_at": posted,
            })
    except Exception as e:
        logging.warning("Greenhouse %s failed: %s", org, e)
    return out


def fetch_lever(org: str) -> list[dict]:
    out = []
    try:
        url = f"https://api.lever.co/v0/postings/{org}?mode=json"
        data = fetch_json(url)
        if not isinstance(data, list):
            return out
        for j in data:
            title = j.get("text", "")
            cats = j.get("categories", {}) or {}
            location = cats.get("location", "") or ""
            workplace = cats.get("workplaceType", "") or cats.get("allLocations", "") or ""
            desc = j.get("descriptionPlain", "") or ""
            if not is_fpga_role_strict(title, desc):
                continue
            if not is_remote(f"{location} {workplace}", desc):
                continue
            posted = str(j.get("createdAt", ""))
            if not is_recent(posted, "Lever"):
                continue
            out.append({
                "url": j.get("hostedUrl", ""),
                "title": title,
                "company": org,
                "location": location or "Remote",
                "source": f"Lever:{org}",
                "posted_at": posted,
            })
    except Exception as e:
        logging.warning("Lever %s failed: %s", org, e)
    return out


def fetch_ashby(org: str) -> list[dict]:
    out = []
    try:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"
        data = fetch_json(url)
        for j in data.get("jobs", []):
            title = j.get("title", "")
            location = j.get("locationName", "") or ""
            desc = j.get("descriptionPlain", "") or ""
            is_remote_flag = bool(j.get("isRemote"))
            if not is_fpga_role_strict(title, desc):
                continue
            if not (is_remote_flag or is_remote(location, desc)):
                continue
            posted = j.get("publishedAt", "") or j.get("updatedAt", "")
            if not is_recent(posted, "Ashby"):
                continue
            out.append({
                "url": j.get("jobUrl", ""),
                "title": title,
                "company": org,
                "location": location or "Remote",
                "source": f"Ashby:{org}",
                "posted_at": posted,
            })
    except Exception as e:
        logging.warning("Ashby %s failed: %s", org, e)
    return out


def fetch_workday(tenant: str, cluster: str, site: str) -> list[dict]:
    """Workday public job-board API.

    Quirks:
    - Requires Referer header pointing to the careers page (else 400).
    - Requires limit <= 20 (else 400).
    - searchText="FPGA remote" forces both keywords (Workday treats it as AND).
    - Listings don't return descriptions, so we filter on title only.
    """
    out = []
    try:
        url = f"https://{tenant}.{cluster}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        body = json.dumps({
            "appliedFacets": {},
            "limit": 20,
            "offset": 0,
            "searchText": "FPGA remote",
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Referer": f"https://{tenant}.{cluster}.myworkdayjobs.com/{site}",
            },
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        for j in data.get("jobPostings", []):
            title = j.get("title", "")
            location = j.get("locationsText", "") or ""
            # Strong: literal FPGA/RTL/Verilog in title -- include regardless.
            # Medium: broader hardware/verification title AND location explicitly
            # marked remote/virtual -- include.
            strong = is_fpga_title_only(title)
            medium = (TITLE_FPGA_RE.search(title) and is_remote(location)
                      and not EXCLUDE_RE.search(f"{title} {location}".lower()))
            if not (strong or medium):
                continue
            posted = j.get("postedOn", "")
            if not is_recent(posted, "Workday"):
                continue
            external_path = j.get("externalPath", "")
            if not external_path:
                continue
            out.append({
                "url": f"https://{tenant}.{cluster}.myworkdayjobs.com/{site}{external_path}",
                "title": title,
                "company": tenant,
                "location": location or "Multiple (per searchText)",
                "source": f"Workday:{tenant}",
                "posted_at": posted,
            })
    except Exception as e:
        logging.warning("Workday %s failed: %s", tenant, e)
    return out


JSEARCH_QUERIES = [
    # (query, date_posted_filter)
    # Free RapidAPI tier ~200 calls/month; 5 queries/day = ~150/month (safe).
    # Embedding "remote" in the query string works MUCH better than the broken
    # remote_jobs_only=true parameter. date_posted=month gives more breadth.
    ("FPGA engineer remote", "month"),
    ("FPGA verification remote", "month"),
    ("RTL design engineer remote", "month"),
    ("FPGA design engineer remote", "month"),
    ("FPGA firmware remote", "month"),
]


def fetch_jsearch() -> list[dict]:
    """JSearch on RapidAPI -- wraps LinkedIn/Indeed/Glassdoor/ZipRecruiter.

    Set RAPIDAPI_KEY env var (and subscribe to JSearch BASIC plan) to enable.
    Free tier: ~200 queries/month; this uses 1 per run.
    """
    api_key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not api_key:
        return []
    out = []
    seen = set()
    for q, date_filter in JSEARCH_QUERIES:
        try:
            params = urllib.parse.urlencode({
                "query": q,
                "page": "1",
                "num_pages": "1",
                "date_posted": date_filter,
                # NOTE: remote_jobs_only=true is broken in JSearch (returns
                # job_is_remote=false for everything). Embed "remote" in query instead.
            })
            url = f"https://jsearch.p.rapidapi.com/search?{params}"
            req = urllib.request.Request(url, headers={
                "x-rapidapi-key": api_key,
                "x-rapidapi-host": "jsearch.p.rapidapi.com",
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            for j in data.get("data", []):
                title = j.get("job_title", "") or ""
                desc = j.get("job_description", "") or ""
                if not is_fpga_role_strict(title, desc):
                    continue
                location_parts = [j.get(k, "") for k in ("job_city", "job_state", "job_country") if j.get(k)]
                location = ", ".join(filter(None, location_parts)) or "Remote"
                if not (j.get("job_is_remote") or is_remote(location, desc)):
                    continue
                posted = j.get("job_posted_at_datetime_utc", "") or ""
                if not is_recent(posted, "JSearch"):
                    continue
                # Prefer the company's direct apply page; skip aggregator-only.
                direct_link = ""
                direct_publisher = ""
                for opt in (j.get("apply_options") or []):
                    if not isinstance(opt, dict):
                        continue
                    if opt.get("is_direct"):
                        direct_link = (opt.get("apply_link") or "").strip()
                        direct_publisher = opt.get("publisher") or "Direct"
                        break
                if not direct_link and j.get("job_apply_is_direct"):
                    direct_link = (j.get("job_apply_link") or "").strip()
                    direct_publisher = j.get("job_publisher") or "Direct"
                if not direct_link or direct_link in seen:
                    continue
                seen.add(direct_link)
                # Strip zero-width and other invisible chars from title (JSearch
                # often returns them around dashes).
                clean_title = re.sub(
                    r"[​-‏‪-‮⁠﻿]", "", title
                )
                out.append({
                    "url": direct_link,
                    "title": clean_title,
                    "company": j.get("employer_name", "") or "",
                    "location": location,
                    "source": f"JSearch:{direct_publisher}",
                    "posted_at": posted,
                })
        except Exception as e:
            logging.warning("JSearch query %r failed: %s", q, e)
    return out


SERPAPI_QUERIES = [
    '"FPGA engineer" "remote" -clearance -secret -"US citizen"',
    '"FPGA" "remote" site:boards.greenhouse.io -clearance',
    '"FPGA" "fully remote" -clearance -secret',
]


def _company_from_url(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        if "greenhouse.io" in host:
            parts = urllib.parse.urlparse(url).path.strip("/").split("/")
            return parts[0] if parts else "(greenhouse)"
        if "lever.co" in host:
            parts = urllib.parse.urlparse(url).path.strip("/").split("/")
            return parts[0] if parts else "(lever)"
        if "ashbyhq.com" in host:
            parts = urllib.parse.urlparse(url).path.strip("/").split("/")
            return parts[0] if parts else "(ashby)"
        return host
    except Exception:
        return ""


def fetch_serpapi() -> list[dict]:
    """Optional broad-web fetcher. Set SERPAPI_KEY env var to enable.

    Free tier: 100 queries/month. We use 3 queries per run (~90/month).
    """
    api_key = os.environ.get("SERPAPI_KEY", "").strip()
    if not api_key:
        return []
    out = []
    seen = set()
    for q in SERPAPI_QUERIES:
        try:
            # tbs=qdr:d restricts Google results to the past 24 hours, matching
            # the rest of the pipeline's freshness contract.
            url = (
                "https://serpapi.com/search.json"
                f"?engine=google&num=20&q={urllib.parse.quote(q)}"
                f"&tbs=qdr:d"
                f"&api_key={urllib.parse.quote(api_key)}"
            )
            data = fetch_json(url)
            for r in data.get("organic_results", []):
                link = (r.get("link") or "").strip()
                if not link or link in seen:
                    continue
                title = r.get("title", "") or ""
                snippet = r.get("snippet", "") or ""
                if not is_fpga(title, snippet):
                    continue
                # Filter out aggregator listing pages (we want the actual job posting)
                low = link.lower()
                if any(k in low for k in ("/search?", "/jobs?", "/q-fpga", "google.com/search")):
                    continue
                seen.add(link)
                out.append({
                    "url": link,
                    "title": title,
                    "company": _company_from_url(link),
                    "location": "Remote (per query)",
                    "source": "SerpAPI:Google",
                    "posted_at": r.get("date", ""),
                })
        except Exception as e:
            logging.warning("SerpAPI query %r failed: %s", q, e)
    return out


def fetch_all() -> list[dict]:
    jobs = []
    has_serpapi = bool(os.environ.get("SERPAPI_KEY", "").strip())
    has_jsearch = bool(os.environ.get("RAPIDAPI_KEY", "").strip())
    total_steps = 7 + (1 if has_serpapi else 0) + (1 if has_jsearch else 0)

    print(f"[1/{total_steps}] Remotive...", flush=True)
    jobs += fetch_remotive()
    print(f"  +{len(jobs)} so far")
    print(f"[2/{total_steps}] Remote OK...", flush=True)
    n0 = len(jobs); jobs += fetch_remoteok()
    print(f"  +{len(jobs) - n0} (total {len(jobs)})")
    print(f"[3/{total_steps}] Hacker News (FPGA + remote, last 7d)...", flush=True)
    n0 = len(jobs); jobs += fetch_hn()
    print(f"  +{len(jobs) - n0} (total {len(jobs)})")
    print(f"[4/{total_steps}] Greenhouse boards ({len(GREENHOUSE_ORGS)} orgs)...", flush=True)
    n0 = len(jobs)
    for org in GREENHOUSE_ORGS:
        jobs += fetch_greenhouse(org)
    print(f"  +{len(jobs) - n0} (total {len(jobs)})")
    print(f"[5/{total_steps}] Lever boards ({len(LEVER_ORGS)} orgs)...", flush=True)
    n0 = len(jobs)
    for org in LEVER_ORGS:
        jobs += fetch_lever(org)
    print(f"  +{len(jobs) - n0} (total {len(jobs)})")
    print(f"[6/{total_steps}] Ashby boards ({len(ASHBY_ORGS)} orgs)...", flush=True)
    n0 = len(jobs)
    for org in ASHBY_ORGS:
        jobs += fetch_ashby(org)
    print(f"  +{len(jobs) - n0} (total {len(jobs)})")
    print(f"[7/{total_steps}] Workday boards ({len(WORKDAY_BOARDS)} tenants)...", flush=True)
    n0 = len(jobs)
    for tenant, cluster, site in WORKDAY_BOARDS:
        jobs += fetch_workday(tenant, cluster, site)
    print(f"  +{len(jobs) - n0} (total {len(jobs)})")
    step = 7
    if has_serpapi:
        step += 1
        print(f"[{step}/{total_steps}] SerpAPI broad-web search...", flush=True)
        n0 = len(jobs); jobs += fetch_serpapi()
        print(f"  +{len(jobs) - n0} (total {len(jobs)})")
    else:
        print("(skip SerpAPI: SERPAPI_KEY env var not set)")
    if has_jsearch:
        step += 1
        print(f"[{step}/{total_steps}] JSearch (LinkedIn/Indeed/Glassdoor via RapidAPI)...", flush=True)
        n0 = len(jobs); jobs += fetch_jsearch()
        print(f"  +{len(jobs) - n0} (total {len(jobs)})")
    else:
        print("(skip JSearch: RAPIDAPI_KEY env var not set)")
    return jobs


def load_db() -> dict:
    if DB_PATH.exists():
        try:
            return json.loads(DB_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_run": None, "jobs": {}, "currently_open_urls": []}


def save_db(db: dict) -> None:
    DB_PATH.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")


def merge(db: dict, new_jobs: list[dict]) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    seen_urls = set()
    for j in new_jobs:
        url = (j.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        if url in db["jobs"]:
            existing = db["jobs"][url]
            existing["last_seen"] = now
            for k in ("title", "company", "location", "source", "posted_at"):
                if j.get(k):
                    existing[k] = j[k]
        else:
            db["jobs"][url] = {**j, "first_seen": now, "last_seen": now}
    db["last_run"] = now
    db["currently_open_urls"] = sorted(seen_urls)
    return db


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>FPGA Remote Jobs &mdash; Live Feed</title>
<style>
:root {
  --bg:#0f1115; --panel:#171a21; --panel-2:#1f242d; --border:#2a313d;
  --text:#e6e9ef; --muted:#8b94a3; --accent:#6ea8fe; --green:#4ade80;
  --yellow:#fbbf24; --red:#f87171; --visited:#585f6b;
}
* { box-sizing: border-box; }
body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  background:var(--bg); color:var(--text); line-height:1.5; }
header { background:linear-gradient(180deg,#1a1f2a,#0f1115); border-bottom:1px solid var(--border);
  padding:24px 32px; }
h1 { margin:0 0 4px 0; font-size:22px; font-weight:600; }
.subtitle { color:var(--muted); font-size:13px; }
.stat-bar { display:flex; gap:12px; flex-wrap:wrap; margin-top:14px; font-size:12px; color:var(--muted); }
.stat { background:var(--panel-2); padding:5px 12px; border-radius:6px; border:1px solid var(--border); }
.stat strong { color:var(--text); margin-right:4px; }
.controls { display:flex; gap:10px; margin-top:14px; flex-wrap:wrap; }
.controls button { background:var(--panel-2); color:var(--text); border:1px solid var(--border);
  border-radius:6px; padding:6px 12px; font-size:12px; cursor:pointer; }
.controls button:hover { border-color:var(--accent); }
main { padding:24px 32px 64px; max-width:1200px; margin:0 auto; }
.filter-bar { display:flex; gap:10px; margin-bottom:20px; align-items:center; flex-wrap:wrap; }
.filter-bar input { background:var(--panel-2); color:var(--text); border:1px solid var(--border);
  border-radius:6px; padding:6px 10px; font-size:13px; min-width:200px; }
.filter-bar label { font-size:12px; color:var(--muted); cursor:pointer; }
.section { margin-bottom:28px; background:var(--panel); border:1px solid var(--border); border-radius:10px; overflow:hidden; }
.sec-head { padding:14px 18px; border-bottom:1px solid var(--border); }
.sec-head h2 { margin:0; font-size:15px; font-weight:600; display:flex; align-items:center; gap:10px; }
.sec-head .count { background:var(--panel-2); color:var(--muted); font-size:12px;
  padding:2px 8px; border-radius:10px; border:1px solid var(--border); }
.dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
.dot.green { background:var(--green); }
.dot.yellow { background:var(--yellow); }
.dot.red { background:var(--red); }
.jobs { padding:0; }
.job { display:block; padding:12px 18px; border-bottom:1px solid var(--border);
  text-decoration:none; color:var(--text); transition:background .1s; position:relative; }
.job:last-child { border-bottom:none; }
.job:hover { background:var(--panel-2); }
.job.visited .title { color:var(--visited); }
.job.applied { background:#0e2a1a; }
.job.applied::before { content:"APPLIED"; position:absolute; right:14px; top:14px;
  font-size:10px; color:var(--green); font-weight:600; letter-spacing:0.5px; }
.job .title { font-size:14px; font-weight:500; padding-right:80px; }
.job .meta { font-size:12px; color:var(--muted); margin-top:4px;
  display:flex; flex-wrap:wrap; gap:12px; }
.job .company { color:var(--accent); }
.job .src { background:var(--panel-2); padding:1px 6px; border-radius:3px;
  border:1px solid var(--border); font-size:10px; }
.actions { font-size:11px; margin-top:6px; display:flex; gap:10px; }
.actions a { color:var(--muted); text-decoration:none; cursor:pointer; }
.actions a:hover { color:var(--accent); }
.empty { padding:24px; text-align:center; color:var(--muted); font-size:13px; }
.footer { margin-top:32px; color:var(--muted); font-size:12px; text-align:center; padding:16px; }
.hidden { display:none !important; }
</style>
</head>
<body>

<header>
  <h1>FPGA Remote Jobs &mdash; Live Feed</h1>
  <div class="subtitle">Auto-aggregated from public job board APIs. URLs are real apply-now links. No clearance-required roles.</div>
  <div class="stat-bar">
    <span class="stat"><strong>__N_NEW__</strong>new in last 24h</span>
    <span class="stat"><strong>__N_OLDER__</strong>still open from earlier</span>
    <span class="stat"><strong>__N_CLOSED__</strong>removed in last 7 days</span>
    <span class="stat">Last run: __LAST_RUN__</span>
  </div>
  <div class="controls">
    <button id="hideApplied">Hide applied</button>
    <button id="resetVisited">Reset visited marks</button>
    <button id="resetApplied">Reset applied marks</button>
    <button id="exportApplied">Export applied list</button>
  </div>
</header>

<main>
  <div class="filter-bar">
    <input id="searchBox" placeholder="Filter by title, company, location...">
    <label><input type="checkbox" id="hideOlder"> Hide "still open from earlier"</label>
    <label><input type="checkbox" id="hideClosed" checked> Hide "no longer posted"</label>
  </div>

  <div class="section">
    <div class="sec-head">
      <h2><span class="dot green"></span> New in last 24 hours <span class="count">__N_NEW__</span></h2>
    </div>
    <div class="jobs" id="new24h">__NEW24__</div>
  </div>

  <div class="section" id="sec-older">
    <div class="sec-head">
      <h2><span class="dot yellow"></span> Still open from earlier <span class="count">__N_OLDER__</span></h2>
    </div>
    <div class="jobs">__OLDER__</div>
  </div>

  <div class="section" id="sec-closed">
    <div class="sec-head">
      <h2><span class="dot red"></span> No longer posted (last 7 days) <span class="count">__N_CLOSED__</span></h2>
    </div>
    <div class="jobs">__CLOSED__</div>
  </div>

  <div class="footer">
    Auto-refreshed daily by GitHub Actions. URLs persist across runs &mdash; new ones appear at top, old ones remain until they drop from feeds. Source: <a href="https://github.com/brandon-maximovich/fpga-jobs-site" style="color:var(--accent);">github.com/brandon-maximovich/fpga-jobs-site</a>
  </div>
</main>

<script>
const VISITED = 'fpga-feed-visited';
const APPLIED = 'fpga-feed-applied';

function load(k) { try { return JSON.parse(localStorage.getItem(k) || '{}'); } catch { return {}; } }
function save(k, v) { localStorage.setItem(k, JSON.stringify(v)); }

const visited = load(VISITED);
const applied = load(APPLIED);

document.querySelectorAll('.job').forEach(a => {
  const url = a.dataset.url;
  if (visited[url]) a.classList.add('visited');
  if (applied[url]) a.classList.add('applied');

  // Inject "Mark applied" link
  const actions = document.createElement('div');
  actions.className = 'actions';
  const mark = document.createElement('a');
  mark.textContent = applied[url] ? 'Unmark applied' : 'Mark as applied';
  mark.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const ap = load(APPLIED);
    if (ap[url]) {
      delete ap[url];
      a.classList.remove('applied');
      mark.textContent = 'Mark as applied';
    } else {
      ap[url] = Date.now();
      a.classList.add('applied');
      mark.textContent = 'Unmark applied';
    }
    save(APPLIED, ap);
  });
  actions.appendChild(mark);
  a.appendChild(actions);

  a.addEventListener('click', () => {
    const v = load(VISITED);
    v[url] = Date.now();
    save(VISITED, v);
    a.classList.add('visited');
  });
});

// Filter
const searchBox = document.getElementById('searchBox');
function applyFilter() {
  const q = searchBox.value.toLowerCase().trim();
  document.querySelectorAll('.job').forEach(a => {
    const text = a.textContent.toLowerCase();
    a.classList.toggle('hidden', q && !text.includes(q));
  });
}
searchBox.addEventListener('input', applyFilter);

document.getElementById('hideApplied').addEventListener('click', () => {
  document.querySelectorAll('.job.applied').forEach(a => a.classList.toggle('hidden'));
});
document.getElementById('hideOlder').addEventListener('change', (e) => {
  document.getElementById('sec-older').classList.toggle('hidden', e.target.checked);
});
document.getElementById('hideClosed').addEventListener('change', (e) => {
  document.getElementById('sec-closed').classList.toggle('hidden', e.target.checked);
});
document.getElementById('hideClosed').dispatchEvent(new Event('change'));

document.getElementById('resetVisited').addEventListener('click', () => {
  if (!confirm('Reset all "visited" marks?')) return;
  localStorage.removeItem(VISITED);
  document.querySelectorAll('.job.visited').forEach(a => a.classList.remove('visited'));
});
document.getElementById('resetApplied').addEventListener('click', () => {
  if (!confirm('Reset all "applied" marks? You will lose your application tracking.')) return;
  localStorage.removeItem(APPLIED);
  document.querySelectorAll('.job.applied').forEach(a => a.classList.remove('applied'));
  document.querySelectorAll('.actions a').forEach(a => a.textContent = 'Mark as applied');
});
document.getElementById('exportApplied').addEventListener('click', () => {
  const ap = load(APPLIED);
  const urls = Object.keys(ap).sort((a, b) => ap[b] - ap[a]);
  const text = urls.map(u => `${new Date(ap[u]).toISOString().slice(0,10)}  ${u}`).join('\\n');
  const blob = new Blob([text], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'fpga-applied-jobs.txt';
  a.click();
});
</script>

</body>
</html>
"""


def render_html(db: dict) -> str:
    now = datetime.now(timezone.utc)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()

    new_24h, older_open, closed_recently = [], [], []
    open_urls = set(db.get("currently_open_urls") or [])

    for url, j in db["jobs"].items():
        if url in open_urls:
            if j.get("first_seen", "") >= cutoff_24h:
                new_24h.append(j)
            else:
                older_open.append(j)
        elif j.get("last_seen", "") >= cutoff_7d:
            closed_recently.append(j)

    new_24h.sort(key=lambda j: j.get("first_seen", ""), reverse=True)
    older_open.sort(key=lambda j: j.get("first_seen", ""), reverse=True)
    closed_recently.sort(key=lambda j: j.get("last_seen", ""), reverse=True)

    def html_escape(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    def jobs_to_html(jobs):
        if not jobs:
            return '<div class="empty">No jobs in this category.</div>'
        rows = []
        for j in jobs:
            rows.append(
                f'<a class="job" href="{html_escape(j["url"])}" target="_blank" '
                f'rel="noopener" data-url="{html_escape(j["url"])}">'
                f'<div class="title">{html_escape(j.get("title","(no title)"))}</div>'
                f'<div class="meta">'
                f'<span class="company">{html_escape(j.get("company",""))}</span>'
                f'<span>{html_escape(j.get("location",""))}</span>'
                f'<span class="src">{html_escape(j.get("source",""))}</span>'
                f'<span>First seen: {html_escape(j.get("first_seen","")[:10])}</span>'
                f'</div></a>'
            )
        return "\n".join(rows)

    last_run = (db.get("last_run") or "")[:19].replace("T", " ")

    return (
        HTML_TEMPLATE
        .replace("__LAST_RUN__", html_escape(last_run))
        .replace("__NEW24__", jobs_to_html(new_24h))
        .replace("__OLDER__", jobs_to_html(older_open))
        .replace("__CLOSED__", jobs_to_html(closed_recently))
        .replace("__N_NEW__", str(len(new_24h)))
        .replace("__N_OLDER__", str(len(older_open)))
        .replace("__N_CLOSED__", str(len(closed_recently)))
    )


def main():
    logging.basicConfig(level=logging.WARNING, format="WARN: %(message)s")
    print("=== FPGA Remote Jobs Aggregator ===")
    new_jobs = fetch_all()
    db = load_db()
    pre_count = len(db["jobs"])
    db = merge(db, new_jobs)
    save_db(db)
    HTML_PATH.write_text(render_html(db), encoding="utf-8")
    print()
    print(f"DB total: {len(db['jobs'])} jobs ({len(db['jobs']) - pre_count} new this run)")
    print(f"Currently open: {len(db.get('currently_open_urls') or [])}")
    print(f"HTML written to: {HTML_PATH}")


if __name__ == "__main__":
    main()
