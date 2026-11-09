# job-alerts

Checks ~200 companies' public job boards every 30 minutes and sends new
India / global-remote tech internships to Telegram.

Hiring systems covered: Greenhouse, Lever, Ashby, SmartRecruiters, Workday,
Oracle Recruiting, Zoho Recruit (RSS), Trakstar Hire, MyNextHire, Keka,
plus custom fetchers for amazon.jobs, jobs.sap.com, Goldman Sachs Higher,
Teradata, Netflix (Eightfold), company-hosted Darwinbox job lists (Upstox, M2P) and plain-HTML
career pages (D. E. Shaw India, Google).
`page_watch` alerts when a page with no job list changes (e.g. NIMS Japan).

Companies whose ATS was verified but can't be read with a simple public
request (bot-protected or JS-only) are listed under `tracked_manually` in
`config.json`, with their official careers URL, so the mapping isn't lost.

- `alerts.py`: fetch, filter, diff against `seen.json`, notify. Standard library only.
- `config.json`: title/location filters (regex) and the company list.
- `.github/workflows/job-alerts.yml`: the schedule.

Run locally: `python3 alerts.py --dry-run`

## Adding a company

Open its careers page, click any job, and look at the URL:

| URL contains | Add to `config.json` under | Example entry |
|---|---|---|
| `boards.greenhouse.io/<slug>` or `gh_jid=` | `greenhouse` | `"stripe"` |
| `jobs.lever.co/<slug>` | `lever` | `"meesho"` |
| `jobs.ashbyhq.com/<slug>` | `ashby` | `"notion"` |
| `jobs.smartrecruiters.com/<Slug>` | `smartrecruiters` | `"Swiggy"` |
| `<tenant>.wd<N>.myworkdayjobs.com/<site>` | `workday` | `{"name": "Intel", "tenant": "intel", "wd": 1, "site": "External"}` |
| `<host>.oraclecloud.com/hcmUI/CandidateExperience/.../sites/<site>` | `oracle` | `{"name": "DTCC", "host": "ebxr.fa.us2.oraclecloud.com", "site": "CX_1", "location_id": 300000000300092}` |

For Oracle, `location_id` is the site's id for "India". Find it by opening
`https://<host>/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&finder=findReqs;siteNumber=<site>,keyword="India"`
and looking for `"Name": "India"` under `locationsFacet`.

A same-named slug on Greenhouse/Lever/Ashby is not proof: `navi` on Ashby
and `porter` on Lever are unrelated companies. Only add a board the
company's own careers page links to.

Then run `python3 alerts.py --dry-run` to check it loads ("0 failed").

## Eligibility check

Every new match is checked against the `profile` block in `config.json`
(graduation year 2028, B.Tech CSE, any location). `eligibility.py` reads the
full posting (ATS API where there is one, else the job page) and looks for
graduation years, "degree completed by …" dates, PhD/Master's-only, final /
pre-final year, branch limits, CGPA cutoffs, duration and dates (flags
semester clashes), and work-authorisation limits.

Each Telegram alert gets one of: ✅ Eligible, 🟢 Likely, ❓ Unclear,
⛔ Not eligible, with the reason, and the list is sorted best-first. When the
posting can't be read, it says so and the link is right there to check.
Skill fit isn't judged.

Check any posting by hand: `python3 eligibility.py <job url>`

## Board health

Greenhouse, Lever, Ashby and SmartRecruiters are official public feeds. The
rest (Workday, Oracle, the Darwinbox lists, HTML pages, ...) read endpoints
or page layouts that can change without notice. So every run records each
board's result in `health.json`, and the first run after 08:00 IST sends a
Telegram summary of boards that errored or returned 0 jobs in the last 24
hours (or "all boards OK").

Workday, Oracle, Amazon and SmartRecruiters only ask for India internships,
so 0 is normal for them; instead they raise an error if the reply stops
looking the way it should, and show up under "Errored". Add `"zero_ok": true`
to a board in `config.json` to stop a board that is really empty from being
listed every day.

## Sources

Each alert is tagged with where it came from: `direct` (a board in
`companies`), `YC`, or `email: LinkedIn/Wellfound/Internshala`. The same
role found twice (same apply link, or same company + title) is sent once,
tagged with every source, and isn't re-sent later via another source.

**YC companies (`yc_auto` in config.json).** Once a day `yc_auto.py`
downloads the yc-oss list of hiring YC companies, keeps those in India or
fully remote, and finds each new company's hiring system from links on its
own website. A board is added only if it answers with jobs and its name
matches the company. Companies with no readable board are listed under
`yc_auto.checked` with the reason and retried after 30 days.
`python3 yc_auto.py --dry-run --force` re-checks everything without saving.
YC's own job pages (ycombinator.com / Work at a Startup) aren't read: their
terms forbid scraping, and many YC startups only post there.

**VC portfolio companies (`vc_auto` in config.json).** The funds' portfolio
*job boards* (Getro for Accel, Consider for Peak XV / Lightspeed) can't be
used: both platforms' terms forbid automated access. Instead `vc_auto.py`
reads each fund's own public portfolio list once a week (Peak XV's company
pages via its sitemap; accel.com/companies), visits each company's own
website, and tracks the company's own board exactly like `yc_auto`. Alerts
from these are tagged `VC: Peak XV` / `VC: Accel`. Lightspeed's site refuses
scripted requests (403), so it's skipped.

**More hiring systems:** Workable, Keka (portal id found automatically),
Zoho Recruit (RSS) and Freshteam (untested: no live Freshteam company was
found). Add e.g. `"workable": [{"name": "Acme", "id": "acme"}]`.

## Email alerts (optional)

`gmail_alerts.py` reads LinkedIn / Wellfound / Internshala alert emails
through the official Gmail API, read-only. The search is limited to the
sender domains in `email_alerts.senders` and the last 3 days, and only job
links are kept. Note the `gmail.readonly` permission itself could read any
mail; the sender limit is enforced by this code. Job pages on LinkedIn etc.
are not fetched (their terms forbid it), so email jobs are judged on the
email text and usually come out "Unclear" with the link.

Setup: run `python3 gmail_setup.py client_secret.json` once (see the steps
in the chat log / below), then store GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET
and GMAIL_REFRESH_TOKEN as GitHub secrets. Check with
`python3 gmail_alerts.py`.
