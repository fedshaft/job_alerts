#!/usr/bin/env python3
"""Decide whether a posting is open to you, from its full description.

Reads the job description (from the ATS API where there is one, otherwise
the public job page) and looks for: graduation years, degree level,
academic year (final / pre-final), branch, CGPA cutoffs, internship
duration and dates, and location / work-authorisation limits.

Verdicts, using the "profile" block in config.json:
    ELIGIBLE      the posting names your graduation year or academic year
    LIKELY        nothing in it rules you out
    UNCLEAR       couldn't read it, or it's ambiguous (link given to check)
    NOT_ELIGIBLE  something rules you out (e.g. "2027 graduates only", PhD)

Skill fit (e.g. "needs CTF experience") is not judged; that's your call.

Check any posting by hand:
    python3 eligibility.py <job url>
"""

import html
import json
import re
import sys
import urllib.parse
from datetime import date
from pathlib import Path

import alerts

ELIGIBLE, LIKELY, UNCLEAR, NOT_ELIGIBLE = "ELIGIBLE", "LIKELY", "UNCLEAR", "NOT_ELIGIBLE"
ORDER = {ELIGIBLE: 0, LIKELY: 1, UNCLEAR: 2, NOT_ELIGIBLE: 3}
MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"


# ---- Getting the description text ----

def _clean(text):
    text = html.unescape(html.unescape(text or ""))
    text = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", ". ", text, flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _page_text(url):
    parser = alerts._Text()
    parser.feed(alerts.http_text(url))
    return " ".join(parser.parts)


def _greenhouse(slug, job):
    d = alerts.http_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job['id']}")
    return _clean(d.get("content"))


def _lever(slug, job):
    d = alerts.http_json(f"https://api.lever.co/v0/postings/{slug}/{job['id']}")
    parts = [d.get("descriptionPlain", "")]
    parts += [f"{l.get('text')}: {_clean(l.get('content'))}" for l in d.get("lists", [])]
    parts += [d.get("additionalPlain", ""), f"Commitment: {(d.get('categories') or {}).get('commitment', '')}"]
    return " ".join(parts)


def _ashby(slug, job):
    d = alerts.http_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    for j in d.get("jobs", []):
        if j["id"] == job["id"]:
            return f"{j.get('descriptionPlain', '')} Employment type: {j.get('employmentType', '')}"
    return ""


def _smartrecruiters(slug, job):
    d = alerts.http_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job['id']}")
    sections = (d.get("jobAd") or {}).get("sections") or {}
    return " ".join(_clean((s or {}).get("text")) for s in sections.values())


def _workday(board, job):
    host = f"https://{board['tenant']}.wd{board['wd']}.myworkdayjobs.com"
    path = job["url"].split(f"/{board['site']}", 1)[1]
    d = alerts.http_json(f"{host}/wday/cxs/{board['tenant']}/{board['site']}{path}")
    info = d.get("jobPostingInfo") or {}
    return f"{_clean(info.get('jobDescription'))} Time type: {info.get('timeType', '')}"


def _oracle(board, job):
    finder = urllib.parse.quote(f'ById;Id="{job["id"]}",siteNumber={board["site"]}', safe="=;,")
    d = alerts.http_json(
        f"https://{board['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        f"?expand=all&onlyData=true&finder={finder}")
    item = (d.get("items") or [{}])[0]
    return " ".join(_clean(item.get(k)) for k in (
        "ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr",
        "CorporateDescriptionStr"))


DETAIL = {
    "greenhouse": lambda b, j: _greenhouse(b, j),
    "lever": lambda b, j: _lever(b, j),
    "ashby": lambda b, j: _ashby(b, j),
    "smartrecruiters": lambda b, j: _smartrecruiters(b, j),
    "workday": _workday,
    "oracle": _oracle,
}


def description(ats, board, job):
    """Best-effort full text of a posting; "" if it can't be read."""
    try:
        if ats in DETAIL:
            text = DETAIL[ats](board, job)
        elif ats in ("page_watch", "email"):
            return ""  # email links point at LinkedIn etc., whose terms forbid fetching pages
        else:
            text = _page_text(job["url"])  # amazon, sap, google, deshaw, ...
        return text if len(text) > 300 else ""  # a JS-only shell page isn't a description
    except Exception as exc:
        print(f"WARN description {ats}/{job['url']}: {exc}", file=sys.stderr)
        return ""


# ---- Rules ----

def _years(s):
    """'2026', '2026-2027', '2026/27', '2026 and 2027' -> {2026, 2027}."""
    out = set()
    for a, b in re.findall(r"\b(20[2-3]\d)(?:\s*(?:-|–|to|/)\s*((?:20)?[2-3]\d))?\b", s):
        a = int(a)
        b = int(b if len(b) == 4 else f"20{b}") if b else a
        out.update(range(a, b + 1) if 0 <= b - a <= 4 else [a])
    return out


YEAR_LIST = r"20[2-3]\d(?:\s*(?:,|/|&|and|or|-|–|to)\s*(?:20)?[2-3]\d)*"
GRAD_PATTERNS = [
    # "2027 graduates", "2026/2027 batch", "2027 pass-outs"
    rf"({YEAR_LIST})\s*(?:graduates?|grads?|batch(?:es)?|pass[- ]?outs?|passing[- ]out|graduating)",
    # "class of 2027", "batch of 2026", "graduating in 2027", "graduation year: 2026 or 2027"
    rf"(?:class|batch)\s+of\s+({YEAR_LIST})",
    rf"(?:graduat\w*|passing out|pass out|completion)[^.]{{0,60}}?\b(?:in|by|between|year|date|of|:)\s*"
    rf"(?:{MONTHS})?\w*\s*({YEAR_LIST})",
]


def classify(job, text, profile, today=None):
    """Return (verdict, [reasons], [notes]). Reasons explain the verdict; notes are FYI."""
    today = today or date.today()
    grad = int(profile["graduation_year"])
    title = job["title"] or text[:150]  # CLI: no title known, use the page start
    blob = f"{title}. {text}"
    low = blob.lower()
    reasons, notes, positive, blockers = [], [], [], []

    # Graduation year
    years = set()
    for p in GRAD_PATTERNS:
        for m in re.finditer(p, blob, re.I):
            years |= _years(m.group(1))
    if years:
        shown = ", ".join(map(str, sorted(years)))
        if grad in years:
            positive.append(f"open to {grad} graduates ({shown})")
        else:
            blockers.append(f"only for {shown} graduates; you graduate {grad}")

    # "degree obtained by summer 2027" = you must have graduated by then
    m = re.search(r"(?:obtained|completed|awarded|graduat\w*|conferred)\s+(?:by|before|no later than)\s+"
                  rf"(?:(?:the\s+)?(?:summer|spring|fall|autumn|winter|end of|{MONTHS})\w*\s+(?:of\s+)?)?(20[2-3]\d)", low)
    if m and grad > int(m.group(1)):
        blockers.append(f"degree must be completed by {m.group(0).split(' by ')[-1].split(' before ')[-1]}; "
                        f"you graduate {grad}")

    # Degree level. "BE"/"BS" only in capitals, so the word "be" doesn't count.
    bachelors = re.search(r"bachelor|b\.?\s?tech|undergrad", low) or re.search(
        r"\bB\.\s?E\b|\bB\.\s?S(?:c)?\b|\bBE\b|\bBS\b|\bBSc\b", blob)
    phd_only = re.search(r"\bph\.?d\b", title, re.I) or (
        re.search(r"(?:pursuing|enrolled in|candidates? for) (?:a )?ph\.?d", low)
        and not bachelors)
    masters_only = re.search(r"(?:pursuing|enrolled in) (?:a )?master'?s", low) and not bachelors
    if phd_only:
        blockers.append("PhD students only")
    elif masters_only:
        blockers.append("Master's students only")
    elif bachelors:
        positive.append("accepts Bachelor's / B.Tech")

    # Academic year. Academic years run Jul-Jun; work out yours today.
    ay_end = today.year + 1 if today.month >= 7 else today.year
    year_of_study = 4 - (grad - ay_end)
    if re.search(r"pre[- ]?final|penultimate", low):
        if year_of_study == 3:
            positive.append("asks for pre-final-year students (that's you)")
        else:
            notes.append("asks for pre-final-year students")
    elif re.search(r"final[- ]year (?:students?|undergrad)", low):
        if year_of_study == 4:
            positive.append("asks for final-year students (that's you)")
        else:
            reasons.append(f"asks for final-year students; you're in year {year_of_study} now "
                           f"(may mean next year's final years, check)")

    # Graduate (not intern) roles
    is_intern = re.search(r"\bintern(ship)?s?\b|\btrainee\b", title, re.I)
    if not is_intern and re.search(r"new grad|graduate|fresher|early career|entry", title, re.I):
        reasons.append(f"full-time role for graduates; you graduate {grad}, check the start date")
    elif is_intern and re.search(r"\bfreshers?\b|recent(?:ly)? graduat|completed (?:your|a) degree|"
                                 r"graduated (?:in|from)", low):
        reasons.append("wording ('freshers' / 'recent graduates') suggests graduates; "
                       "worth asking if they take pre-final-year students")

    # Branch
    m = re.search(r"((?:[\w/&]+\s+){1,2})branch(?:es)?\s+only|only\s+(?:for\s+)?((?:[\w/&]+\s+){1,2})branch", low)
    if m:
        what = (m.group(1) or m.group(2) or "").strip()
        if re.search(r"circuit|comput|\bcs\b|cse|\bit\b|information tech|software", what):
            notes.append(f"branch limit '{what} branches': CSE qualifies")
        else:
            blockers.append(f"branch limit '{what} branches' may exclude CSE")

    # CGPA
    m = re.search(r"(?:cgpa|gpa|cpi)[^.\d]{0,25}(\d(?:\.\d+)?)\s*(?:/\s*10)?\s*(?:and above|or above|\+|minimum)?", low) \
        or re.search(r"(?:minimum|at least)\s*(\d(?:\.\d+)?)\s*(?:cgpa|gpa|cpi)", low)
    if m and 5 <= float(m.group(1)) <= 10:
        notes.append(f"needs CGPA {m.group(1)}+")

    # Duration / dates / full-time during semester
    dur = re.search(r"(\d{1,2})[- ]?(?:\(\w+\)\s*)?months?\b(?:[\s-]+[\w-]+){0,3}?[\s-]+(?:intern|program)", low) \
        or re.search(r"(?:duration|period|internship)\W{0,3}(?:is|of|:)?\s*(\d{1,2})\s*months?", low)
    span = re.search(rf"\b({MONTHS})\w*\.?\s*(20\d\d)?\s*(?:-|–|to)\s*({MONTHS})\w*\.?\s*(20\d\d)", low)
    if dur:
        notes.append(f"{dur.group(1)}-month internship")
    if span:
        notes.append(f"runs {span.group(1).title()} {span.group(2) or ''} to "
                     f"{span.group(3).title()} {span.group(4)}".replace("  ", " "))
    if (dur and int(dur.group(1)) >= 5) or (span and span.group(1) in ("jan", "feb", "jul", "aug")):
        notes.append("overlaps a semester; check your college allows it")

    # Location / work authorisation (you're fine with any location, so FYI only)
    if re.search(r"authori[sz]ed to work|work authori[sz]ation|right to work|"
                 r"(?:no|not|unable to) (?:provide )?(?:visa )?sponsor", low):
        notes.append("needs local work authorisation / no visa sponsorship")
    if re.search(r"\bon-?site\b|in[- ]office|work from (?:the|our) \w+ office", low):
        notes.append("onsite")

    if blockers:
        return NOT_ELIGIBLE, blockers + reasons, notes
    if not text:
        return UNCLEAR, ["couldn't read the full description; check the link"], notes
    if reasons:
        return UNCLEAR, reasons + positive, notes
    if any(p.startswith(("open to", "asks for")) for p in positive):
        return ELIGIBLE, positive, notes
    return LIKELY, positive or ["no graduation-year, degree or year-of-study limit found"], notes


def check(ats, board, job, profile):
    return classify(job, description(ats, board, job), profile)


def format_verdict(verdict, reasons, notes):
    icon = {ELIGIBLE: "✅", LIKELY: "🟢", UNCLEAR: "❓", NOT_ELIGIBLE: "⛔"}[verdict]
    line = f"{icon} {verdict.replace('_', ' ').title()}: {'; '.join(reasons)}"
    return line + (f"\nℹ️ {'; '.join(dict.fromkeys(notes))}" if notes else "")


def _guess_board(url):
    """For the CLI: map a pasted job URL to (ats, board, job)."""
    m = re.search(r"greenhouse\.io/([\w-]+)/jobs/(\d+)", url) or re.search(r"[?&]gh_jid=(\d+)", url)
    if m and m.lastindex == 2:
        return "greenhouse", m.group(1), {"id": m.group(2), "title": "", "url": url}
    m = re.search(r"jobs\.lever\.co/([\w-]+)/([\w-]{36})", url)
    if m:
        return "lever", m.group(1), {"id": m.group(2), "title": "", "url": url}
    m = re.search(r"jobs\.smartrecruiters\.com/([\w-]+)/(\d+)", url)
    if m:
        return "smartrecruiters", m.group(1), {"id": m.group(2), "title": "", "url": url}
    return "html", None, {"id": url, "title": "", "url": url}


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python3 eligibility.py <job url>")
    profile = json.loads(Path(alerts.CONFIG_FILE).read_text())["profile"]
    ats, board, job = _guess_board(sys.argv[1])
    try:  # the title decides intern vs graduate role, so look it up where we can
        if ats == "greenhouse":
            job["title"] = alerts.http_json(
                f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job['id']}")["title"]
        elif ats == "lever":
            job["title"] = alerts.http_json(f"https://api.lever.co/v0/postings/{board}/{job['id']}")["text"]
        elif ats == "smartrecruiters":
            job["title"] = alerts.http_json(
                f"https://api.smartrecruiters.com/v1/companies/{board}/postings/{job['id']}")["name"]
    except Exception:
        pass
    if job["title"]:
        print(job["title"])
    print(format_verdict(*check(ats, board, job, profile)))
    print(job["url"])


if __name__ == "__main__":
    main()
