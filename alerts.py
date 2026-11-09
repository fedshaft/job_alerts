#!/usr/bin/env python3
"""Watch public job boards and send new matching roles to Telegram.

Supported hiring systems: Greenhouse, Lever, Ashby, SmartRecruiters, Workday,
Oracle Recruiting, Zoho Recruit, Trakstar, MyNextHire, Keka, plus a few
company-specific career sites. Each reads the same public data the official
careers page shows; no logins, and nothing behind a bot challenge.

Usage:
    python3 alerts.py            # check boards, alert on new matches, save seen.json
    python3 alerts.py --dry-run  # print matches, send nothing, save nothing

Env vars (both needed to send; without them it just prints):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Standard library only, so there is nothing to install.
"""

import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from html.parser import HTMLParser
from xml.etree import ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
SEEN_FILE = ROOT / "seen.json"
SEEN_KEEP_DAYS = 180
USER_AGENT = "job-alerts/1.0"
# Some custom career sites refuse non-browser clients.
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
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


def fetch_zoho_rss(board):
    """Zoho Recruit career sites publish an RSS feed: board = {"name", "url"}."""
    req = urllib.request.Request(board["url"], headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=25) as resp:
        root = ET.fromstring(resp.read())
    jobs = []
    for item in root.iter("item"):
        title = html.unescape(item.findtext("title") or "")
        link = (item.findtext("link") or "").split("?")[0]
        desc = re.sub(r"<[^>]+>", " ", html.unescape(item.findtext("description") or ""))
        m = re.search(r"Location:\s*(.+?)(?:\s{2,}|$)", desc)
        jobs.append(job(item.findtext("guid") or link, title,
                        m.group(1) if m else board.get("location", ""), link))
    return jobs


def fetch_trakstar(board):
    """Trakstar Hire (formerly Recruiterbox). board = client name, e.g. "whatfix101",
    or {"name", "id": client name}."""
    slug = board["id"] if isinstance(board, dict) else board
    jobs, offset = [], 0
    while True:
        data = http_json(f"https://jsapi.recruiterbox.com/v1/openings"
                         f"?client_name={slug}&limit=20&offset={offset}")  # >20 truncates
        for o in data.get("objects", []):
            loc = o.get("location") or {}
            where = join_locations([loc.get("city"), loc.get("state"), loc.get("country")])
            jobs.append(job(o["id"], o["title"], where, o["hosted_url"]))
        offset += 20
        if offset >= (data.get("meta") or {}).get("total", 0) or offset >= 500:
            return jobs


def fetch_mynexthire(board):
    """MyNextHire career iframe: board = {"name", "id" (client), "url" (public careers page)}."""
    data = http_json(
        f"https://{board['id']}.mynexthire.com/employer/careers/reqlist/get",
        {"source": "careers", "code": "", "filterByBuId": -1},
    )
    return [
        # No per-job public link exists; point at the official listing page.
        job(r["reqId"], r["reqTitle"],
            join_locations([r.get("location")] + (r.get("locationGroup") or [])),
            board["url"])
        for r in data.get("reqDetailsBOList", [])
    ]


class _Links(HTMLParser):
    """Collect (href, visible text) for every <a> in a page."""

    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            a = dict(attrs)
            self._href, self._text = a.get("href"), []
            # Icon-only links (e.g. Google careers) carry the title here instead.
            self._aria = re.sub(r"^learn more about\s+", "", a.get("aria-label") or "", flags=re.I)

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            text = " ".join("".join(self._text).split())
            if not text or GENERIC_LINK_TEXT.match(text):
                text = self._aria or text
            self.links.append((self._href, text))
            self._href = None


class _Text(HTMLParser):
    """Visible text of a page (no scripts/styles), for change detection."""

    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def http_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode(resp.headers.get_content_charset() or "utf-8", "replace")


GENERIC_LINK_TEXT = re.compile(r"^(learn more|apply( now)?|view( job| details)?|read more|details|share|email|more)$", re.I)


def fetch_html(board):
    """Custom career page whose job links are in the raw HTML.

    board = {"name", "url", "link_pattern", "location",
             "id_pattern" (regex with one group: the job id inside the href),
             "title_pattern" (optional regex with one group: the clean title),
             "link_base" (optional, when the page sets <base href>)}.
    The first non-generic link text for a job is its title ("Learn more"
    links to the same job are ignored). "location" is used as-is, so only
    point this at a page that is already filtered to India.
    """
    page = http_text(board["url"])
    parser = _Links()
    parser.feed(page)
    pattern = re.compile(board.get("link_pattern", "."), re.IGNORECASE)
    id_pattern = re.compile(board["id_pattern"]) if board.get("id_pattern") else None
    title_pattern = re.compile(board["title_pattern"]) if board.get("title_pattern") else None
    jobs = {}
    for href, text in parser.links:
        if not text or GENERIC_LINK_TEXT.match(text) or not pattern.search(href):
            continue
        url = urllib.parse.urljoin(board.get("link_base", board["url"]), href)
        m = id_pattern.search(href) if id_pattern else None
        job_id = m.group(1) if m else url
        if job_id in jobs:
            continue
        if title_pattern:
            t = title_pattern.search(text)
            text = t.group(1).strip() if t else text
        jobs[job_id] = job(job_id, text, board.get("location", ""), url)
    return list(jobs.values())


def fetch_oracle(board):
    """Oracle Recruiting Cloud (Fusion HCM) career sites.

    board = {"name", "host" (e.g. "jpmc.fa.oraclecloud.com"), "site" (siteNumber,
    e.g. "CX_1001"), "location_id" (the site's id for "India"), "keywords"
    (optional list, default intern/graduate), "site_path" or "job_url" (optional,
    public link with {id}, when the site has its own domain)}.
    Like Workday: searches by keyword, filtered to India on the server.
    location_id is stored in config because the facet list in replies only
    shows the top ~10 locations, so India can be missing from it (BNY, Uber).
    """
    base = (f"https://{board['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            "?onlyData=true&expand=requisitionList.secondaryLocations&finder=")

    def search(keyword, location_id=None, offset=0):
        finder = f'findReqs;siteNumber={board["site"]},limit=25,offset={offset},keyword="{keyword}"'
        if location_id:
            finder += f",locationId={location_id}"
        data = http_json(base + urllib.parse.quote(finder, safe="=;,"))
        return (data.get("items") or [{}])[0]

    link = board.get("job_url") or (
        f"https://{board['host']}/hcmUI/CandidateExperience/en/sites/"
        f"{board.get('site_path', board['site'])}/job/{{id}}")
    jobs = {}
    for keyword in board.get("keywords", ["intern", "graduate"]):
        india = board.get("location_id")
        if not india:
            first = search(keyword)
            ids = [f["Id"] for f in first.get("locationsFacet", []) if f.get("Name") == "India"]
            if not ids:
                continue  # nothing in India for this keyword
            india = ids[0]
        offset = 0
        while offset < 400:
            page = search(keyword, india, offset)
            reqs = page.get("requisitionList", [])
            for r in reqs:
                locs = [r.get("PrimaryLocation")] + [
                    s.get("Name") for s in r.get("secondaryLocations") or []]
                jobs[r["Id"]] = job(r["Id"], r["Title"], join_locations(locs),
                                    link.format(id=r["Id"]))
            offset += 25
            if not reqs or offset >= page.get("TotalJobsCount", 0):
                break
    return list(jobs.values())


def fetch_amazon(board):
    """amazon.jobs public search JSON, India only. board = {"name", "keywords"?}."""
    jobs = {}
    for keyword in board.get("keywords", ["intern", "graduate"]):
        offset = 0
        while offset < 500:
            data = http_json(
                "https://www.amazon.jobs/en/search.json?normalized_country_code%5B%5D=IND"
                f"&base_query={urllib.parse.quote(keyword)}&result_limit=100&offset={offset}"
                "&sort=recent")
            for j in data.get("jobs", []):
                jobs[j["id_icims"]] = job(j["id_icims"], j["title"], j.get("normalized_location", ""),
                                          "https://www.amazon.jobs" + j["job_path"])
            offset += 100
            if offset >= data.get("hits", 0):
                break
    return list(jobs.values())


def _as_list(value):
    """Some APIs send lists as Python-repr strings ("['Mumbai']"); accept both."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.startswith("["):
        return re.findall(r"'([^']*)'", value)
    return [value] if value else []


def fetch_keka(board):
    """Keka Hire career portal. board = {"name", "id" (subdomain), "embed" (portal id)}."""
    data = http_json(f"https://{board['id']}.keka.com/careers/api/embedjobs/default/active/{board['embed']}")
    jobs = []
    for j in data:
        locs = j.get("jobLocations") or []
        if isinstance(locs, str):
            locs = [{"city": c} for c in re.findall(r"'city': '([^']*)'", locs)]
        where = join_locations([l.get("city") or l.get("name") for l in locs] + ["India"])
        jobs.append(job(j["id"], j["title"], where,
                        f"https://{board['id']}.keka.com/careers/jobdetails/{j['id']}"))
    return jobs


def fetch_darwinbox_json(board):
    """A company's own JSON copy of its Darwinbox jobs (Upstox, M2P).

    Darwinbox's API itself is behind a Cloudflare challenge; these companies
    serve the same records from their own careers site. board = {"name",
    "list_url", "apply_url" (with {id})}. Only jobs flagged for the careers page.
    """
    data = http_json(board["list_url"])
    rows = data.get("data") if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = next((v for v in rows.values() if isinstance(v, list)), [])
    jobs = []
    for r in rows or []:
        if str(r.get("post_on_careers_page")) != "1":
            continue  # drafts / internal-only reqs (e.g. "Dummy Designation Name")
        where = join_locations(_as_list(r.get("location_city")) + [r.get("location_country")])
        jobs.append(job(r["job_id"], r["job_title"], where, board["apply_url"].format(id=r["job_id"])))
    return jobs


GS_QUERY = ("query GetRoles($searchQueryInput: RoleSearchQueryInput!){roleSearch("
            "searchQueryInput:$searchQueryInput){totalCount items{roleId jobTitle "
            "locations{city country}}}}")


def fetch_goldman(board):
    """Goldman Sachs "Higher" site: the public GraphQL its search page uses. India only."""
    jobs = {}
    for experience in board.get("experiences", ["CAMPUS", "EARLY_CAREER"]):
        page = 0
        while page < 20:
            body = {"operationName": "GetRoles", "query": GS_QUERY, "variables": {"searchQueryInput": {
                "page": {"pageSize": 50, "pageNumber": page},
                "filters": [{"filterCategoryType": "LOCATION",
                             "filters": [{"filter": "India", "subFilters": []}]}],
                "experiences": [experience], "searchTerm": ""}}}
            res = http_json("https://api-higher.gs.com/gateway/api/v1/graphql", body)["data"]["roleSearch"]
            for r in res["items"]:
                where = join_locations(f"{l.get('city')}, {l.get('country')}" for l in r.get("locations") or [])
                num = str(r["roleId"]).split("_")[0]
                jobs[r["roleId"]] = job(r["roleId"], r["jobTitle"], where, f"https://higher.gs.com/roles/{num}")
            page += 1
            if page * 50 >= res.get("totalCount", 0):
                break
    return list(jobs.values())


TERADATA_QUERY = ("query($first:Int,$start:Int){searchJobs: searchGoogleJobDiscovery(query:\"\","
                  "first:$first,start:$start,filters:{}){results{totalCount nodes{id key title "
                  "places{nodes{name}}}}}}")


def fetch_teradata(board):
    """careers.teradata.com (Gr8 People): the site's own GraphQL. All jobs, 100 per page."""
    jobs, start = [], 0
    while start < 1000:
        res = http_json("https://careers.teradata.com/graphql",
                        {"query": TERADATA_QUERY, "variables": {"first": 100, "start": start}})
        r = res["data"]["searchJobs"]["results"]
        for n in r["nodes"]:
            where = join_locations(p["name"] for p in (n.get("places") or {}).get("nodes", []))
            jobs.append(job(n["key"], n["title"], where,
                            f"https://careers.teradata.com/jobs/{n['key']}"))
        start += 100
        if start >= r.get("totalCount", 0):
            return jobs
    return jobs


def fetch_sap(board):
    """jobs.sap.com: the listing page embeds its results as JSON.

    board = {"name", "url"} where url already filters to India, e.g.
    https://jobs.sap.com/en/jobs/?keyword=intern&country=India
    """
    page = http_text(board["url"]).replace('\\"', '"')
    jobs = {}
    for m in re.finditer(r'\{"id":"(\d+)","title":"((?:[^"\\]|\\.)*)","url":"([^"]+)"(.*?)\}', page):
        loc = re.search(r'"locations":"((?:[^"\\]|\\.)*)"', m.group(4))
        title = json.loads(f'"{m.group(2)}"')
        jobs[m.group(1)] = job(m.group(1), title, loc.group(1) if loc else "India",
                               urllib.parse.urljoin(board["url"], m.group(3)))
    return list(jobs.values())


def fetch_page_watch(board):
    """Pages that announce programs rather than list jobs (e.g. NIMS).

    Emits one pseudo-job whose id is a hash of the page text, so any change
    to the page shows up as a new item.
    """
    parser = _Text()
    parser.feed(http_text(board["url"]))
    text = " ".join(parser.parts)
    # Optionally hash only the main content, so menus/footers don't cause false alerts.
    start, end = board.get("start"), board.get("end")
    if start and start in text:
        text = text[text.index(start):]
    if end and end in text:
        text = text[:text.index(end)]
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    return [job(digest, f"Page updated: {board['name']}", board.get("location", ""), board["url"])]


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
    "zoho_rss": fetch_zoho_rss,
    "trakstar": fetch_trakstar,
    "mynexthire": fetch_mynexthire,
    "oracle": fetch_oracle,
    "amazon": fetch_amazon,
    "sap": fetch_sap,
    "keka": fetch_keka,
    "darwinbox_json": fetch_darwinbox_json,
    "goldman": fetch_goldman,
    "teradata": fetch_teradata,
    "html": fetch_html,
    "page_watch": fetch_page_watch,
}


def board_name(ats, board):
    return board["name"] if isinstance(board, dict) else board


def board_id(ats, board):
    if not isinstance(board, dict):
        return board
    return board.get("id") or board.get("tenant") or board["name"]


def fetch_board(ats, board):
    """Return (ats, board, jobs), with jobs=None if the board failed."""
    try:
        return ats, board, FETCHERS[ats](board)
    except Exception as exc:  # one broken board must not stop the rest
        print(f"WARN {ats}/{board_name(ats, board)}: {exc}", file=sys.stderr)
        return ats, board, None


# ---- Filtering, state, notifications ----

class Matcher:
    """Title/location rules. A category in config.json can override any of them."""

    def __init__(self, cfg, overrides=None):
        rules = {k: cfg[k] for k in ("title_role", "title_tech", "title_exclude", "location")}
        rules.update(overrides or {})
        flags = re.IGNORECASE
        self.role = re.compile(rules["title_role"], flags)
        self.tech = re.compile(rules["title_tech"], flags)
        self.exclude = re.compile(rules["title_exclude"], flags)
        self.location = re.compile(rules["location"], flags)

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
    categories = cfg.get("categories", {})
    matchers = {None: Matcher(cfg)}
    matchers.update({name: Matcher(cfg, rules) for name, rules in categories.items()})

    boards = [(ats, b) for ats, items in cfg["companies"].items() for b in items]
    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(lambda b: fetch_board(*b), boards))

    failed = [f"{a}/{board_name(a, b)}" for a, b, jobs in results if jobs is None]
    found = []  # (key, company, job)
    for ats, board, jobs in results:
        category = board.get("category") if isinstance(board, dict) else None
        label = board_name(ats, board) + (f" [{category}]" if category else "")
        for j in jobs or []:
            # page_watch items aren't job titles; any change is worth a look.
            if ats == "page_watch" or matchers[category](j):
                key = f"{ats}:{board_id(ats, board)}:{j['id']}"
                found.append((key, label, j))

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
