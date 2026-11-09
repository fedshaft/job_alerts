# job-alerts

Checks ~200 companies' public job boards every 30 minutes and sends new
India / global-remote tech internships to Telegram.

Hiring systems covered: Greenhouse, Lever, Ashby, SmartRecruiters, Workday,
Oracle Recruiting, Zoho Recruit (RSS), Trakstar Hire, MyNextHire, Keka,
plus custom fetchers for amazon.jobs, jobs.sap.com, Goldman Sachs Higher,
Teradata, company-hosted Darwinbox job lists (Upstox, M2P) and plain-HTML
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
