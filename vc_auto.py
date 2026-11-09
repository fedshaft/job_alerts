"""Track the portfolio companies of Indian VC funds through their own job boards.

The funds' portfolio JOB boards (Getro for Accel, Consider for Peak XV and
Lightspeed) can't be used: both platforms' terms forbid automated access.
So instead this reads each fund's own public portfolio list (company name
and website), then, exactly like yc_auto, finds each company's hiring
system from links on the company's OWN website and tracks that board.
Results go in config.json under "vc_auto", refreshed weekly.

Sources:
    Peak XV  peakxv.com sitemap -> /companies/<slug> pages -> website link
    Accel    accel.com sitemap -> /companies/<slug> pages; India = partner in an Indian office
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


INDIA_OFFICES = re.compile(r"bangalore|bengaluru|delhi|mumbai|gurgaon|gurugram|india", re.I)


def accel(cache=None):
    """[(name, slug, website)] for Accel's India portfolio.

    accel.com/companies only embeds ~200 of ~780 companies, so this walks the
    company pages listed in Accel's sitemap (once; cached afterwards). Accel
    doesn't tag a company's country on its page, so "India" means the Accel
    partner on the company is based in an Indian office.
    cache = {slug: (name, website, is_india)}.
    """
    cache = cache or {}
    sitemap = alerts.http_text("https://www.accel.com/sitemap.xml")
    pages = sorted(set(re.findall(r"<loc>(https://accel\.com/companies/[\w-]+)</loc>", sitemap)))

    def one(url):
        slug = url.rsplit("/", 1)[1]
        if slug in cache:
            return (cache[slug][0], slug, cache[slug][1], cache[slug][2])
        time.sleep(0.5)
        try:
            page = alerts.http_text(url.replace("://accel.com", "://www.accel.com"))
        except Exception:
            return None
        flat = page.replace('\\"', '"')
        offices = re.findall(r'"basedIn":\{"_id":"office-[^"]+","name":"([^"]+)"', flat)
        # Accel's pages all share one <title>, so the name comes from the address.
        name = slug.replace("-", " ").title()
        parser = alerts._Links()
        parser.feed(page)
        root = re.sub(r"[^a-z0-9]", "", slug.lower())[:5]
        externals = [h for h, _ in parser.links if h.startswith("http") and not SOCIAL.search(h)
                     and "accel.com" not in h]
        # The company's own site is the external link that looks like its name.
        site = next((h for h in externals if root in re.sub(r"[^a-z0-9]", "", h.lower())), "")
        return name, slug, site, any(INDIA_OFFICES.search(o) for o in offices)

    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = [r for r in pool.map(one, pages) if r]
    return [(n, s, w) for n, s, w, india in rows if india and w], rows


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
            cache = {k.split(":", 1)[1]: (n.get("name", ""), n.get("website", ""), n.get("india", True))
                     for k, n in vc["checked"].items() if k.startswith(fund + ":") and n.get("website")}
            companies = source(cache)
            if isinstance(companies, tuple):  # accel: (india companies, every page seen)
                companies, rows = companies
                for name, slug, site, india in rows:  # remember non-India ones so they aren't refetched
                    if not india and site:
                        vc["checked"].setdefault(f"{fund}:{slug}", {"date": "9999-12-31", "name": name,
                                                                   "website": site, "india": False,
                                                                   "status": "not India"})
        except Exception as exc:
            reports.append(f"{fund}: portfolio list failed ({exc})")
            continue
        todo = sorted((c for c in companies
                       if vc["checked"].get(f"{fund}:{c[1]}", {}).get("date", "") < retry),
                      key=lambda c: vc["checked"].get(f"{fund}:{c[1]}", {}).get("date", ""))
        todo = todo[:MAX_CHECKS]  # spread re-checks out so no single run gets slow

        def one(c):
            name, slug, site = c
            found = yc_auto.find_board({"slug": slug, "name": name, "website": site})
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
            note = {"date": today.isoformat(), "name": name, "website": site, "india": True}
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
