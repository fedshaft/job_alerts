"""Track the portfolio companies of Indian VC funds through their own job boards.

The funds' portfolio JOB boards (Getro for Accel, Consider for Peak XV and
Lightspeed) can't be used: both platforms' terms forbid automated access.
So instead this reads each fund's own public portfolio list (company name
and website), then, exactly like yc_auto, finds each company's hiring
system from links on the company's OWN website and tracks that board.
Results go in config.json under "vc_auto", refreshed weekly.

Sources:
    Peak XV  peakxv.com sitemap -> /companies/<slug> pages -> website link
    Accel    accel.com/companies (the page's embedded company list)
    Lightspeed: lsvp.com refuses scripted requests (403), so it's skipped.

    python3 vc_auto.py            # refresh now and save config.json
    python3 vc_auto.py --dry-run  # refresh, print, don't save
"""

import html
import json
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import alerts
import yc_auto

REFRESH_DAYS = 7
MAX_CHECKS = 60  # company websites checked per fund per refresh
RECHECK_DAYS = 30
SOCIAL = re.compile(r"linkedin|twitter|x\.com|facebook|instagram|youtube|medium\.com|crunchbase|"
                    r"apple\.com|play\.google|peakxv|sequoia|website-files|intellimize|webflow", re.I)


def peakxv(cache=None):
    """[(name, slug, website)] from Peak XV's company pages (listed in its sitemap).
    cache = {slug: (name, website)} from earlier runs, so only new pages are fetched."""
    cache = cache or {}
    sitemap = alerts.http_text("https://www.peakxv.com/sitemap.xml")
    pages = sorted(set(re.findall(r"<loc>(https://www\.peakxv\.com/companies/[\w-]+)</loc>", sitemap)))

    def one(url):
        slug = url.rsplit("/", 1)[1]
        if slug in cache:
            return (cache[slug][0], slug, cache[slug][1])
        time.sleep(0.5)  # be gentle: one fund site, a few hundred pages, once a week
        try:
            page = alerts.http_text(url)
        except Exception:
            return None
        parser = alerts._Links()
        parser.feed(page)
        site = next((h for h, _ in parser.links if h.startswith("http") and not SOCIAL.search(h)), "")
        title = re.search(r"<title>([^<|]+)", page)  # "1mg | Peak XV Partners Portfolio Company"
        name = html.unescape(title.group(1)).strip() if title else slug.replace("-", " ").title()
        return name, slug, site

    with ThreadPoolExecutor(max_workers=3) as pool:
        return [r for r in pool.map(one, pages) if r and r[2]]


def accel(cache=None):
    """[(name, slug, website)] from the company list embedded in accel.com/companies."""
    page = alerts.http_text("https://www.accel.com/companies").replace('\\"', '"')
    out = {}
    for chunk in page.split('{"_createdAt"')[1:]:
        if '"_type":"company"' not in chunk[:400]:
            continue
        site = re.search(r'"websiteUrl":"([^"]+)"', chunk)
        slug = re.search(r'"slug":\{"_type":"slug","current":"([^"]+)"', chunk)
        if not (site and slug):
            continue
        # Many nested objects (tags, founders) also have "name"; the company's
        # own name is the last one before its slug.
        names = re.findall(r'"name":"([^"]+)"', chunk[:slug.start()])
        if names:
            out[slug.group(1)] = (names[-1], slug.group(1), site.group(1))
    return list(out.values())


FUNDS = {"Peak XV": peakxv, "Accel": accel}


def update(cfg, today=None, force=False):
    """Refresh cfg["vc_auto"] in place, at most weekly. Returns a short report."""
    today = today or date.today()
    vc = cfg.setdefault("vc_auto", {"last_run": "", "boards": {}, "checked": {}})
    last = vc.get("last_run") or "2000-01-01"
    if not force and date.fromisoformat(last) > today - timedelta(days=REFRESH_DAYS):
        return ""
    known = {(a, alerts.board_id(a, b).lower()) for a, items in cfg["companies"].items() for b in items}
    for a, items in (cfg.get("yc_auto") or {}).get("boards", {}).items():
        known |= {(a, b["id"].lower()) for b in items}
    retry = (today - timedelta(days=RECHECK_DAYS)).isoformat()
    reports = []
    for fund, source in FUNDS.items():
        try:
            cache = {k.split(":", 1)[1]: (n.get("name", ""), n.get("website", ""))
                     for k, n in vc["checked"].items() if k.startswith(fund + ":") and n.get("website")}
            companies = source(cache)
        except Exception as exc:
            reports.append(f"{fund}: portfolio list failed ({exc})")
            continue
        todo = sorted((c for c in companies
                       if vc["checked"].get(f"{fund}:{c[1]}", {}).get("date", "") < retry),
                      key=lambda c: vc["checked"].get(f"{fund}:{c[1]}", {}).get("date", ""))
        todo = todo[:MAX_CHECKS]  # spread re-checks out so no single run gets slow

        def one(c):
            name, slug, site = c
            found = yc_auto.detect(site)
            if not found:
                return c, None, None
            ats, board_slug, where, tld = found
            if not yc_auto.looks_like_company(board_slug, {"slug": slug, "name": name, "website": site}):
                return c, found, "review"
            if ats not in alerts.FETCHERS or (ats, board_slug.lower()) in known:
                return c, found, "known" if (ats, board_slug.lower()) in known else None
            entry = dict(yc_auto.board_entry(name, ats, board_slug, tld), fund=fund)
            return c, found, entry if yc_auto._verify(ats, entry) else None

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(one, todo))
        added = 0
        for (name, slug, site), found, entry in results:
            note = {"date": today.isoformat(), "name": name, "website": site}
            if found:
                note.update(ats=found[0], slug=found[1], evidence=found[2])
            if entry == "review":
                note["status"] = "needs review: board name doesn't match the company"
            elif entry == "known":
                note["status"] = "already tracked"
            elif entry:
                vc["boards"].setdefault(found[0], [])
                if not any(b["id"] == entry["id"] for b in vc["boards"][found[0]]):
                    vc["boards"][found[0]].append(entry)
                    known.add((found[0], entry["id"].lower()))
                    added += 1
                note["status"] = "added"
            else:
                note["status"] = "no readable board" if found else "no ATS link on site"
            vc["checked"][f"{fund}:{slug}"] = note
        reports.append(f"{fund}: {len(companies)} companies, checked {len(todo)}, added {added}")
    vc["last_run"] = today.isoformat()
    return "VC portfolios: " + "; ".join(reports)


def boards(cfg):
    """(ats, board) pairs from vc_auto. Small startups often have 0 openings."""
    return [(ats, dict(b, zero_ok=True))
            for ats, items in (cfg.get("vc_auto") or {}).get("boards", {}).items() for b in items]


if __name__ == "__main__":
    cfg = json.loads(alerts.CONFIG_FILE.read_text())
    print(update(cfg, force=True))
    if "--dry-run" not in sys.argv:
        alerts.CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
