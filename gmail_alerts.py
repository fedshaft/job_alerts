#!/usr/bin/env python3
"""Internship listings from LinkedIn / Wellfound / Internshala alert emails.

Uses the official Gmail API with the read-only scope (gmail.readonly).
Only messages from the senders in config.json ("email_alerts.senders")
from the last few days are searched, and only job links are kept. The
scope itself can read any mail, so the sender limit is enforced here, in
the search query; nothing else is read.

Env vars (from `python3 gmail_setup.py`):
    GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN

    python3 gmail_alerts.py --dry-run   # print what would be extracted
"""

import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request

import alerts

API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Job links per source: (source, regex on the unwrapped URL, group = job id).
JOB_LINKS = [
    ("LinkedIn", re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")),
    ("Wellfound", re.compile(r"wellfound\.com/(?:jobs|l)/(\d+)")),
    ("Internshala", re.compile(r"internshala\.com/(?:internship|job)/detail/([\w-]+)")),
]


def access_token():
    body = urllib.parse.urlencode({
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
        "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", body, timeout=20) as r:
        return json.load(r)["access_token"]


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def _bodies(part):
    """All text/html and text/plain bodies in a message, decoded."""
    out = []
    if part.get("mimeType") in ("text/html", "text/plain") and part.get("body", {}).get("data"):
        out.append(base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", "replace"))
    for p in part.get("parts") or []:
        out += _bodies(p)
    return out


def _unwrap(url):
    """Follow redirect-style tracking links that carry the real URL in a parameter."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("url", "u", "redirect", "target", "dest"):
        if key in q and q[key][0].startswith("http"):
            return q[key][0]
    return url


def extract(html_text):
    """[(source, job_id, title, url)] from one email body."""
    parser = alerts._Links()
    parser.feed(html_text)
    found = {}
    for href, text in parser.links:
        url = _unwrap(href)
        for source, pattern in JOB_LINKS:
            m = pattern.search(url)
            if m and text and len(text) > 3 and (source, m.group(1)) not in found:
                clean = url.split("?")[0]
                if source == "LinkedIn":
                    clean = f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
                found[(source, m.group(1))] = (source, m.group(1), text, clean)
    return list(found.values())


def fetch_email_jobs(cfg, days=3):
    """Job dicts from alert emails. Raises if Gmail isn't configured."""
    senders = cfg.get("email_alerts", {}).get("senders", [])
    if not senders:
        return []
    token = access_token()
    q = f"from:({' OR '.join(senders)}) newer_than:{days}d"
    listing = _get(f"{API}/messages?maxResults=50&q={urllib.parse.quote(q)}", token)
    jobs = {}
    for ref in listing.get("messages", []):
        msg = _get(f"{API}/messages/{ref['id']}?format=full", token)
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        for body in _bodies(msg["payload"]):
            for source, job_id, title, url in extract(body):
                jobs[(source, job_id)] = dict(alerts.job(f"{source}:{job_id}", title, "", url),
                                              source=source, email_subject=headers.get("subject", ""))
    return list(jobs.values())


def main():
    cfg = json.loads(alerts.CONFIG_FILE.read_text())
    jobs = fetch_email_jobs(cfg)
    print(f"{len(jobs)} job links in alert emails from the last 3 days")
    for j in jobs:
        print(f"  {j['source']}: {j['title']} | {j['url']}  (email: {j['email_subject'][:60]})")


if __name__ == "__main__":
    main()
