# job-alerts

Checks ~170 companies' public job boards every 30 minutes and sends new
India / global-remote tech internships to Telegram.

Hiring systems covered: Greenhouse, Lever, Ashby, SmartRecruiters, Workday.

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

Then run `python3 alerts.py --dry-run` to check it loads ("0 failed").
