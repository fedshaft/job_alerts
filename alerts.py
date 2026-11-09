#!/usr/bin/env python3
"""Watch public job boards and send new matching roles to Telegram.

Supported hiring systems: Greenhouse, Lever, Ashby, SmartRecruiters, Workday.
All of them expose a public JSON feed of open jobs (the same data their
careers pages show), so no logins are involved.

Usage:
    python3 alerts.py            # check boards, alert on new matches, save seen.json
    python3 alerts.py --dry-run  # print matches, send nothing, save nothing

Env vars (both needed to send; without them it just prints):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Standard library only, so there is nothing to install.
"""

import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
SEEN_FILE = ROOT / "seen.json"
SEEN_KEEP_DAYS = 180
USER_AGENT = "job-alerts/1.0"
INDIA = re.compile(
    r"india|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|gurgaon|gurugram"
    r"|noida|delhi|kolkata|ahmedabad|jaipur|kochi|coimbatore",
    re.IGNORECASE,
)


def http_json(url, payload=None):
    """GET (or POST JSON if payload is given) and parse the JSON reply."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.load(resp)


def job(id_, title, location, url):
    return {"id": str(id_), "title": title.strip(), "location": location, "url": url}


def join_locations(locs):
    return " | ".join(dict.fromkeys(l.strip() for l in locs if l and l.strip()))


# ---- One fetcher per hiring system. Each returns a list of job dicts. ----

def fetch_greenhouse(slug):
    data = http_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [
        job(j["id"], j["title"], (j.get("location") or {}).get("name", ""), j["absolute_url"])
        for j in data.get("jobs", [])
    ]


def fetch_lever(slug):
    data = http_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        locs = [cats.get("location")] + (cats.get("allLocations") or [])
        jobs.append(job(j["id"], j["text"], join_locations(locs), j["hostedUrl"]))
    return jobs


def fetch_ashby(slug):
    data = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    jobs = []
    for j in data.get("jobs", []):
        if not j.get("isListed", True):
            continue
        locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
        jobs.append(job(j["id"], j["title"], join_locations(locs), j["jobUrl"]))
    return jobs


def fetch_smartrecruiters(slug):
    """India-only on the server side (country=in); paged 100 at a time."""
    jobs, offset = [], 0
    while True:
        data = http_json(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
            f"?country=in&limit=100&offset={offset}"
        )
        for p in data.get("content", []):
            loc = (p.get("location") or {}).get("fullLocation") or "India"
            jobs.append(job(p["id"], p["name"], loc,
                            f"https://jobs.smartrecruiters.com/{slug}/{p['id']}"))
        offset += 100
        if offset >= data.get("totalFound", 0) or offset >= 1000:
            return jobs


def _india_facets(facets):
    """Find Workday location-filter values that are in India.

    Returns {facetParameter: [ids]}, preferring a country-level filter.
    Filter ids differ per company, so they're looked up on every run.
    """
    found = {}

    def walk(items, param=None):
        for f in items:
            p = f.get("facetParameter", param)
            for v in f.get("values", []):
                if "values" in v:
                    walk([v], p)
                elif p and "location" in p.lower() and INDIA.search(v.get("descriptor") or ""):
                    found.setdefault(p, []).append(v["id"])

    walk(facets)
    for param in found:
        if "country" in param.lower():
            return {param: found[param]}
    return dict([next(iter(found.items()))]) if found else {}


def fetch_workday(board):
    """board = {"name", "tenant", "wd", "site"}. Asks Workday for India 'intern' jobs only."""
    host = f"https://{board['tenant']}.wd{board['wd']}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{board['tenant']}/{board['site']}/jobs"
    query = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "intern"}

    first = http_json(api, query)
    facets = _india_facets(first.get("facets", []))
    if not facets:
        return []  # nothing in India for this search
    query["appliedFacets"] = facets

    jobs = []
    while query["offset"] < 400:
        data = http_json(api, query)
        for p in data.get("jobPostings", []):
            path = p["externalPath"]
            loc = p.get("locationsText") or ""
            if not INDIA.search(loc):
                loc = f"{loc} (India)".strip()  # e.g. "3 Locations"; the India filter guarantees one
            jobs.append(job(path.rsplit("_", 1)[-1], p["title"], loc,
                            f"{host}/en-US/{board['site']}{path}"))
        query["offset"] += 20
        if query["offset"] >= data.get("total", 0):
            break
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
}


def board_name(ats, board):
    return board["name"] if isinstance(board, dict) else board


def board_id(ats, board):
    return board["tenant"] if isinstance(board, dict) else board


def fetch_board(ats, board):
    """Return (ats, board, jobs), with jobs=None if the board failed."""
    try:
        return ats, board, FETCHERS[ats](board)
    except Exception as exc:  # one broken board must not stop the rest
        print(f"WARN {ats}/{board_name(ats, board)}: {exc}", file=sys.stderr)
        return ats, board, None


# ---- Filtering, state, notifications ----

class Matcher:
    def __init__(self, cfg):
        flags = re.IGNORECASE
        self.role = re.compile(cfg["title_role"], flags)
        self.tech = re.compile(cfg["title_tech"], flags)
        self.exclude = re.compile(cfg["title_exclude"], flags)
        self.location = re.compile(cfg["location"], flags)

    def __call__(self, job):
        t = job["title"]
        return (
            bool(self.role.search(t))
            and bool(self.tech.search(t))
            and not self.exclude.search(t)
            and bool(self.location.search(job["location"]))
        )


def load_seen():
    if not SEEN_FILE.exists():
        return None  # first run
    return json.loads(SEEN_FILE.read_text())


def save_seen(seen, still_open):
    """Drop old entries, but never one that is still listed (or it would re-alert)."""
    cutoff = (date.today() - timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff or k in still_open}
    SEEN_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")


def format_job(company, job):
    return (
        f"<b>{html.escape(company)}</b>: {html.escape(job['title'])}\n"
        f"{html.escape(job['location'])}\n"
        f"{html.escape(job['url'])}"
    )


def send_telegram(token, chat_id, text):
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        if not json.load(resp).get("ok"):
            raise RuntimeError("Telegram did not accept the message")


def chunk_messages(header, items, limit=3800):
    """Split into messages under Telegram's 4096-char limit."""
    messages, current = [], header
    for item in items:
        if len(current) + len(item) + 2 > limit:
            messages.append(current)
            current = ""
        current += ("\n\n" if current else "") + item
    messages.append(current)
    return messages


def main():
    dry_run = "--dry-run" in sys.argv
    cfg = json.loads(CONFIG_FILE.read_text())
    matches = Matcher(cfg)

    boards = [(ats, b) for ats, items in cfg["companies"].items() for b in items]
    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(lambda b: fetch_board(*b), boards))

    failed = [f"{a}/{board_name(a, b)}" for a, b, jobs in results if jobs is None]
    found = []  # (key, company, job)
    for ats, board, jobs in results:
        for j in jobs or []:
            if matches(j):
                key = f"{ats}:{board_id(ats, board)}:{j['id']}"
                found.append((key, board_name(ats, board), j))

    seen = load_seen()
    first_run = seen is None
    seen = seen or {}
    new = [(k, c, j) for k, c, j in found if k not in seen]

    print(f"{len(boards)} boards, {len(failed)} failed, "
          f"{len(found)} matching open roles, {len(new)} new")
    if failed:
        print("  failed:", ", ".join(failed))
    for _, company, j in new:
        print(f"  NEW {company}: {j['title']} | {j['location']} | {j['url']}")

    if dry_run:
        return

    if new:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if token and chat_id:
            header = (f"Currently open ({len(new)}):" if first_run
                      else f"New roles ({len(new)}):")
            items = [format_job(c, j) for _, c, j in new]
            for msg in chunk_messages(header, items):
                send_telegram(token, chat_id, msg)  # raises -> seen not saved -> retried next run
        else:
            # Don't mark jobs as seen that were never delivered.
            print("Telegram env vars not set; printed only, seen.json not updated.")
            return

    today = date.today().isoformat()
    for key, _, _ in found:
        seen.setdefault(key, today)
    save_seen(seen, {key for key, _, _ in found})

    # Fail loudly (GitHub emails you) if many boards break, e.g. an API change.
    if len(failed) > len(boards) // 4:
        sys.exit(f"Too many boards failed: {failed}")


if __name__ == "__main__":
    main()
