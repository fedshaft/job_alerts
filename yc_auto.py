"""Add hiring YC companies in India (or fully remote) to config.json.

Once a day: download the yc-oss list of hiring YC companies, keep those
located in India or fully remote, and for each company not checked yet,
find its hiring system from links on the company's OWN website (homepage
and careers page). A same-named slug on an ATS is not evidence, so nothing
is guessed. Found boards go in config.json under "yc_auto"; companies
with no readable board are remembered too (so they aren't re-checked
every day; they're retried after RECHECK_DAYS).

YC's own job pages are not read: ycombinator.com's terms forbid scraping.
"""

import json
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import alerts

YC_LIST = "https://yc-oss.github.io/api/companies/hiring.json"
RECHECK_DAYS = 30
MAX_CHECKS = 60  # company websites checked per run

# href patterns -> (ats, how to build the board entry)
ATS_LINKS = [
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)"),
    ("lever", r"jobs\.lever\.co/([\w.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([\w.%-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)"),
    ("workable", r"apply\.workable\.com/([\w-]+)"),
    ("workable", r"([\w-]+)\.workable\.com"),
    ("freshteam", r"([\w-]+)\.freshteam\.com"),
    ("zoho_recruit", r"([\w-]+)\.zohorecruit\.(com|in|eu)"),
    ("keka", r"([\w-]+)\.keka\.com/careers"),
]
# Recruiting agencies post for unnamed clients; they aren't employers.
SKIP_COMPANIES = {"weekday"}
NOT_SLUGS = {"www", "apply", "jobs", "careers", "embed", "api", "app", "help", "static"}
CAREER_HREF = re.compile(r"career|jobs|join|hiring|work-with-us|open-roles|positions", re.I)


def _links(url):
    page = alerts.http_text(url)
    parser = alerts._Links()
    parser.feed(page)
    return page, [urllib.parse.urljoin(url, h) for h, _ in parser.links]


def detect(website):
    """Return (ats, slug, evidence_url) from the company's own site, or None."""
    if not website:
        return None
    if not website.startswith("http"):
        website = "https://" + website
    try:
        page, links = _links(website)
    except Exception:
        return None
    pages = [(website, page, links)]
    careers = [l for l in links if CAREER_HREF.search(l)][:2]
    for c in careers:
        if any(re.search(p, c) for _, p in ATS_LINKS):
            continue  # already an ATS link; matched below
        try:
            p, ls = _links(c)
            pages.append((c, p, ls))
        except Exception:
            pass
    found = _scan(pages)
    if found:
        return found
    # Careers pages that aren't linked as such (or are built with JavaScript):
    # try the usual addresses on the company's own domain.
    parts = urllib.parse.urlparse(website)
    host = parts.netloc.removeprefix("www.")
    for guess in (f"{parts.scheme}://{parts.netloc}/careers", f"{parts.scheme}://{parts.netloc}/jobs",
                  f"https://careers.{host}", f"https://jobs.{host}"):
        try:
            p, ls = _links(guess)
        except Exception:
            continue
        found = _scan([(guess, p, ls)])
        if found:
            return found
    return None


def _scan(pages):
    for where, page, links in pages:
        haystack = " ".join(links) + " " + page  # embeds (iframes/scripts) are not <a> links
        for ats, pattern in ATS_LINKS:
            for m in re.finditer(pattern, haystack, re.I):
                slug = urllib.parse.unquote(m.group(1)).strip(".")
                if slug.lower() not in NOT_SLUGS:
                    tld = m.group(2) if ats == "zoho_recruit" else None
                    return ats, slug, where, tld
    return None


def _board_title(ats, slug):
    """The name a public board gives itself, or ""."""
    try:
        if ats == "greenhouse":
            return alerts.http_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}").get("name", "")
        url = {"lever": f"https://jobs.lever.co/{slug}", "ashby": f"https://jobs.ashbyhq.com/{slug}"}[ats]
        m = re.search(r"<title>([^<]+)</title>", alerts.http_text(url))
        return re.sub(r"^(jobs at|careers at)\s+|\s*(jobs|careers)$", "", m.group(1).strip(), flags=re.I) if m else ""
    except Exception:
        return ""


def probe(company):
    """Fallback when the site shows no ATS link: look for a Greenhouse/Lever/Ashby
    board named after the company's domain. A same-named board is NOT enough
    (e.g. "navi" on Ashby is an unrelated US startup), so it must also call
    itself by the company's name AND list at least one job in India."""
    domain = re.sub(r"^https?://(www\.)?", "", company.get("website") or "").split("/")[0]
    root = domain.split(".")[0].lower()
    if len(root) < 4:
        return None
    name = _n(company["name"])
    for ats in ("greenhouse", "lever", "ashby"):
        title = _n(_board_title(ats, root))
        if not title or not (title.startswith(name[:6]) or name.startswith(title[:6])):
            continue
        try:
            jobs = alerts.FETCHERS[ats](root)
        except Exception:
            continue
        if any(alerts.INDIA.search(j["location"]) for j in jobs):
            return ats, root, f"board named '{_board_title(ats, root)}' with India jobs (no link on site)", None
    return None


def find_board(company):
    """detect() on the company's own site, else the verified probe()."""
    return detect(company.get("website")) or probe(company)


def _n(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def looks_like_company(slug, company):
    """A site can link to another company's board (a customer, an investor).
    Only accept a slug that resembles the company's YC slug, name or domain."""
    s = _n(slug)
    domain = _n(re.sub(r"^https?://(www\.)?", "", company.get("website", "")).split("/")[0].rsplit(".", 1)[0])
    names = [_n(company["slug"]), _n(company["name"]), domain]
    return any(x and (s.startswith(x[:4]) or x.startswith(s[:4])) for x in names)


def board_entry(name, ats, slug, tld=None):
    if ats == "zoho_recruit":
        return {"name": name, "id": slug, "domain": f"{slug}.zohorecruit.{tld}"}
    return {"name": name, "id": slug}


def _verify(ats, entry):
    """The board must answer and contain at least one job to be worth adding."""
    try:
        return bool(alerts.fetch_board(ats, entry)[2])
    except Exception:
        return False


def update(cfg, today=None, force=False):
    """Refresh cfg["yc_auto"] in place. Returns a short report string."""
    today = today or date.today()
    yc = cfg.setdefault("yc_auto", {"last_run": "", "boards": {}, "checked": {}})
    if not force and yc.get("last_run") == today.isoformat():
        return ""
    companies = alerts.http_json(YC_LIST)
    wanted = [c for c in companies
              if "India" in (c.get("regions") or []) or "India" in (c.get("all_locations") or "")
              or "Fully Remote" in (c.get("regions") or [])]
    retry = (today - timedelta(days=RECHECK_DAYS)).isoformat()
    todo = sorted((c for c in wanted if c["slug"] not in SKIP_COMPANIES
                   and yc["checked"].get(c["slug"], {}).get("date", "") < retry),
                  key=lambda c: yc["checked"].get(c["slug"], {}).get("date", ""))[:MAX_CHECKS]  # spread re-checks

    def one(c):
        found = find_board(c)
        if not found:
            return c, None, None
        ats, slug, where, tld = found
        if not looks_like_company(slug, c):
            return c, (ats, slug, where), "review"
        if ats not in alerts.FETCHERS:
            return c, (ats, slug, where), None
        entry = board_entry(c["name"], ats, slug, tld)
        return c, (ats, slug, where), entry if _verify(ats, entry) else None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(one, todo))

    added = []
    for c, found, entry in results:
        note = {"date": today.isoformat(), "website": c.get("website", "")}
        if found:
            note.update(ats=found[0], slug=found[1], evidence=found[2])
        if entry == "review":
            note["status"] = "needs review: board name doesn't match the company"
        elif entry:
            yc["boards"].setdefault(found[0], [])
            if not any(b["id"] == entry["id"] for b in yc["boards"][found[0]]):
                yc["boards"][found[0]].append(entry)
                added.append(f"{c['name']} ({found[0]})")
            note["status"] = "added"
        else:
            note["status"] = "no readable board" if found else "no ATS link on site"
        yc["checked"][c["slug"]] = note
    yc["last_run"] = today.isoformat()
    return (f"YC: {len(wanted)} India/remote companies, checked {len(todo)}, "
            f"added {len(added)}" + (f": {', '.join(added)}" if added else ""))


def boards(cfg):
    """(ats, board) pairs from yc_auto, skipping ones already tracked directly."""
    direct = {(a, alerts.board_id(a, b).lower()) for a, items in cfg["companies"].items() for b in items}
    out = []
    for ats, items in (cfg.get("yc_auto") or {}).get("boards", {}).items():
        for b in items:
            if (ats, b["id"].lower()) not in direct:
                # Small startups are often between openings: 0 jobs isn't a breakage.
                out.append((ats, dict(b, zero_ok=True)))
    return out


if __name__ == "__main__":
    cfg = json.loads(alerts.CONFIG_FILE.read_text())
    print(update(cfg, force="--force" in sys.argv))
    if "--dry-run" not in sys.argv:
        alerts.CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
