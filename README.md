# seo-audit-agents

A small multi-agent pipeline that audits websites for SEO problems.

```
data/websites.csv
      |
      v
 Agent 1  File Reader      read + validate URLs
      |
      v
 Agent 2  Crawler          BeyondSEO CLI crawl -> reports/<site>/crawl/ -> issues.csv
      |
      v
 Agent 3  LLM Analyzer          -- or --   Agent 4  Rule-Based Analyzer
 local Ollama (Qwen3): summary,            deterministic SEO rules: severity
 severity, recommendations,                (High/Medium/Low), recommendations,
 priorities                                priority actions. No LLM, no network.
      |                                          |
      v                                          v
              PDF report   reports/<site>/report.pdf
```

Exactly one analysis agent runs per audit - Agent 3 (LLM) or Agent 4 (rule-based) - chosen with the
`enable_agent3` / `enable_agent4` config options (or `--engine` on the command line):

```
Agent1 -> Agent2 -> Agent3 -> PDF     (enable_agent3=true,  enable_agent4=false)  [default]
Agent1 -> Agent2 -> Agent4 -> PDF     (enable_agent3=false, enable_agent4=true)
```

Every website gets its own folder containing the raw crawl output, `issues.csv` and `report.pdf`.

## Project structure

```
seo-audit-agents/
├── data/websites.csv          input: one URL per row, column named "url"
├── agents/
│   ├── agent1_reader.py       Agent 1 - reads and validates URLs (pandas)
│   ├── agent2_crawler.py      Agent 2 - runs BeyondSEO, normalizes issues.csv
│   ├── agent3_analyzer.py     Agent 3 - Ollama/Qwen3 analysis (+ its own rule-based fallback)
│   ├── agent4_rule_engine.py  Agent 4 - deterministic rule-based SEO analysis (no LLM)
│   └── models.py              dataclasses shared by the agents
├── utils/
│   ├── logging_setup.py       console + rotating file logging
│   ├── helpers.py             slugs, safe paths, text truncation
│   └── pdf_report.py          ReportLab PDF builder
├── reports/                   output (one folder per website)
├── config.py                  configuration (defaults, config.json, env vars)
├── config.example.json        copy to config.json and edit
├── main.py                    entry point
└── requirements.txt
```

## Web interface (Flask + Bootstrap)

Alongside `python main.py`, the project now ships a browser UI that runs the **exact same
agents** in the background:

```
app.py                 Flask application factory + error pages
routes/
├── dashboard.py        home page (system status, recent audits) + /history
├── audit.py             new-audit form, live progress page/API, results page
└── downloads.py          serves issues.csv / report.pdf / run_summary.csv per audit
services/
├── job_store.py          thread-safe, disk-backed audit job queue (reports/_jobs/<id>/)
├── audit_runner.py        runs Agent1 -> Agent2 -> Agent3/4 -> PDF in a background thread
├── url_validation.py      friendly validation of the URL typed into the form
├── errors.py               turns pipeline exceptions into a title/message/hint
└── health.py                 cheap BeyondSEO / Ollama reachability checks for the dashboard
templates/               Jinja2 + Bootstrap 5 pages (base layout, form, progress, results, ...)
static/
├── vendor/                Bootstrap 5.3.3 + Bootstrap Icons 1.11.3, vendored (no CDN, no Node)
├── css/app.css             small custom layer (severity colors, log console, stage list)
└── js/app.js                client-side polling of /audit/<id>/status.json, no framework
```

### Run it

```bash
pip install -r requirements.txt      # now also installs Flask
python app.py                        # http://127.0.0.1:5000
# or, while developing:
FLASK_DEBUG=1 python app.py
```

Nothing here talks to a CDN or needs `npm install` - Bootstrap and Bootstrap Icons are vendored
as plain files under `static/vendor/`.

### What you get

- **Dashboard** (`/`) - BeyondSEO/Ollama status, quick stats, recent audits.
- **New Audit** (`/audit/new`) - enter a URL, optionally cap pages crawled or force
  Agent 3 / Agent 4, and start the audit.
- **Progress** (`/audit/<id>`) - a stage tracker (Agent 1 -> preflight -> Agent 2 ->
  Agent 3/4 -> PDF), a progress bar and a live log tail, polling
  `/audit/<id>/status.json` every 1.5s; redirects to the results page automatically.
- **Results** (`/audit/<id>/results`) - pages crawled, total/medium/low issue counts, crawl
  status, executive summary, severity breakdown, priority actions, recommendations, and a full
  issue-group table, all in Bootstrap cards/tables.
- **Downloads** - buttons for `issues.csv`, `report.pdf`, `run_summary.csv`, served from a
  per-job snapshot so they don't change if you audit the same site again later.
- **History** (`/history`) - every audit ever run through the web UI, newest first.

### How it fits together with the CLI

The web layer adds **no audit logic**. `services/audit_runner.py` calls
`FileReaderAgent`, `CrawlerAgent`, `AnalysisAgent` / `RuleEngineAgent` and `build_pdf` -
the same classes `main.py` uses - and reuses `main.py`'s own `select_analysis_engine` and
`write_run_summary` helpers. A website audited from the browser is written to
`reports/<site>/` exactly like a CLI run; the job itself (status, log, a copy of its
deliverables) lives in `reports/_jobs/<job_id>/` so downloads keep working even if that
site is audited again later. Jobs run one at a time (crawls and local LLM inference are
both heavy) via a single background worker thread; starting a second audit while one is
running queues it.

### Error handling

`services/errors.py` turns pipeline exceptions into a plain-language title, message and
next step, shown on the results page when a job fails:

- an invalid or unreachable URL (before the job is even created, via `url_validation.py`)
- BeyondSEO missing / failing to start / crawling nothing
- a crawl that runs past its timeout
- Ollama unreachable, the model missing, or a request that times out (falls back to
  Agent 4 automatically when `llm_fallback` is `true`, with a warning banner)
- a finished audit whose `issues.csv` / `report.pdf` / `run_summary.csv` did not get
  written (download buttons report "not found" instead of erroring)

If the Flask process restarts while a job is queued/running, that job is marked failed on
the next startup (it can never resume) instead of hanging forever "in progress".

### Notes for a real deployment

`python app.py` runs Flask's own dev server, which prints a warning that it is not for
production. For anything beyond local/demo use, run it behind a WSGI server, e.g.:

```bash
pip install gunicorn      # Linux/macOS; on Windows use waitress instead
gunicorn -w 1 -b 0.0.0.0:8000 app:app     # -w 1: keep a single worker (see below)
```

Keep the worker count at 1 (or add a proper task queue such as Celery/RQ if you need more):
`AuditRunner` schedules jobs on an in-process thread, and `JobStore`'s in-memory cache of
active jobs is per-process, so multiple worker processes would each run and show a different
subset of jobs.

## Prerequisites

1. **Python 3.12+**
2. **BeyondSEO** (the crawler, used as an external CLI)
3. **Ollama** with a Qwen3 model (the analysis LLM)

### 1. Install this project

```bash
cd seo-audit-agents
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Install BeyondSEO

BeyondSEO lives at <https://github.com/beyondtahir/beyondseo>. Clone it anywhere and run its setup:

```bash
git clone https://github.com/beyondtahir/beyondseo.git
cd beyondseo
python3 scripts/setup.py             # Windows: py -3 scripts/setup.py
# HTTP-only, no Chromium download:   python3 scripts/setup.py --http-only
```

Setup creates BeyondSEO's own virtual environment; its launcher `scripts/run.py` uses it automatically, so you never need to activate it. Tell this project how to start BeyondSEO by creating `config.json` (copy `config.example.json`):

```json
{
  "beyondseo_cmd": ["python3", "/absolute/path/to/beyondseo/scripts/run.py"]
}
```

On Windows use forward slashes and the `py` launcher, e.g. `["py", "-3", "C:/tools/beyondseo/scripts/run.py"]`.
If `beyondseo` is already on your PATH, the default (`["beyondseo"]`) works without any config.

### 3. Install Ollama and Qwen3

Install Ollama from <https://ollama.com>, then:

```bash
ollama pull qwen3:8b
ollama serve                         # skip if the Ollama app is already running
```

Any Qwen3 size works (`qwen3:4b`, `qwen3:14b`, ...). Set it with `ollama_model` in `config.json` or `--model`.

## Usage

Put the websites you want to audit into `data/websites.csv`:

```csv
url
https://example.com
https://example.org
```

> Only audit sites you own or have permission to crawl.

Run:

```bash
python main.py
```

| Flag | Meaning |
|------|---------|
| `--input FILE` | CSV with a `url` column (default `data/websites.csv`) |
| `--output-dir DIR` | where per-website folders are created (default `reports/`) |
| `--max-pages N` | max pages BeyondSEO crawls per website |
| `--model NAME` | Ollama model, e.g. `qwen3:14b` |
| `--limit N` | only process the first N websites (handy for a test run) |
| `--skip-crawl` | reuse the existing `crawl/` folders; re-runs only analysis + PDF |
| `--no-llm` | when Agent 3 runs, skip Ollama and use its own rule-based fallback |
| `--engine agent3\|agent4` | override which analysis agent runs this audit (default: from config) |
| `--config FILE` | use a specific JSON config file |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

`--skip-crawl` is the fast loop while you tweak prompts or the PDF layout: no re-crawling.

Exit codes: `0` all sites succeeded, `1` at least one site failed, `2` the run could not start (bad config, missing CSV, BeyondSEO not found).

## Output

```
reports/
├── run_summary.csv                 one row per website: status, issue count, analysis source
└── example-com/
    ├── issues.csv                  normalized issues, most severe first   <- report 1
    ├── report.pdf                  the audit report                       <- report 2
    ├── crawl.log                   raw BeyondSEO stdout/stderr
    └── crawl/                      raw BeyondSEO output (pages.csv, links.csv, report.md, summary.json, ...)
logs/seo_audit.log                  detailed run log (DEBUG level)
```

`issues.csv` columns: `url, code, severity, confidence, evidence, action`. Severity is one of `Critical / High / Medium / Low / Info / Unrated`.

The PDF contains: at-a-glance metrics, executive summary, severity chart and table, priority actions, SEO recommendations, issue groups, an appendix of issues, and a scope/limitations section.

## Configuration

Precedence (later wins): defaults in `config.py` -> `config.json` -> environment variables `SEO_<OPTION>` (e.g. `SEO_OLLAMA_MODEL=qwen3:14b`) -> command-line flags.

| Option | Default | Notes |
|--------|---------|-------|
| `enable_agent3` | `true` | run Agent 3 (LLM analysis) when selected; at least one of `enable_agent3`/`enable_agent4` must be true |
| `enable_agent4` | `false` | run Agent 4 (rule-based analysis) when selected; if both are `true`, Agent 4 runs (faster, no LLM) unless `--engine` says otherwise |
| `beyondseo_cmd` | `["beyondseo"]` | how to launch BeyondSEO (list of arguments) |
| `crawl_max_pages` | `100` | BeyondSEO `--max-pages` |
| `crawl_mode` | `auto` | `auto`, `http` (no Chromium needed) or `browser` |
| `crawl_include_www` | `true` | include the www / non-www twin of the host |
| `crawl_timeout_seconds` | `1800` | hard limit per website |
| `crawl_extra_args` | `[]` | extra BeyondSEO flags, e.g. `["--exclude", "/tag/"]` |
| `crawl_fresh` | `true` | `true`: wipe the old `crawl/` folder; `false`: `--resume` it |
| `ollama_host` | `http://localhost:11434` | |
| `ollama_model` | `qwen3:8b` | |
| `ollama_timeout_seconds` | `600` | |
| `ollama_temperature` | `0.2` | low = more consistent output |
| `ollama_num_ctx` | `8192` | context window requested from Ollama |
| `ollama_think` | `false` | Qwen3 "thinking" mode: slower, usually not needed |
| `llm_max_retries` | `2` | extra attempts when the reply is invalid or the call fails |
| `llm_fallback` | `true` | write a rule-based analysis instead of failing when the LLM is unavailable |
| `llm_max_issue_groups` | `40` | issue groups sent to the model |
| `pdf_page_size` | `A4` | `A4` or `LETTER` |
| `pdf_max_appendix_rows` | `50` | issue rows echoed in the PDF appendix |

## How the agents work

**Agent 1 - File Reader.** Reads the CSV with pandas (handles the Excel BOM and a `URL`/`url` header), skips blanks and duplicates, adds `https://` when missing, rejects invalid URLs with a warning, and gives each site a unique folder name.

**Agent 2 - Crawler.** Runs `beyondseo crawl <url> --out reports/<site>/crawl --max-pages N --mode M ...` as a subprocess with a timeout, keeps the raw output in `crawl/`, then reads BeyondSEO's `issues.csv` (or `issues.json`), maps the columns, normalizes severity, removes duplicates, sorts and writes `reports/<site>/issues.csv`. A crawl that extracts no pages is reported as a failure instead of a "0 issues" report.

**Agent 3 - LLM Analyzer.** pandas groups the issues by severity and issue code (counts, affected pages, example URLs). Only that compact digest goes to Qwen3 via Ollama with a JSON schema, so even very large issue lists fit. The reply is validated and cleaned (`<think>` blocks and code fences are stripped) and retried on failure. **The severity breakdown is counted with pandas, not by the LLM**, so the numbers are always exact. If Ollama is down, the model is not pulled, or the reply stays invalid, a deterministic rule-based analysis is used and the PDF says so.

**Agent 4 - Rule-Based Analyzer.** Reads the same `issues.csv` written by Agent 2 and needs no LLM or network access. Every issue's code/evidence is matched against a predefined table of SEO rules (missing title, missing meta description, missing H1, broken links, redirect chains, canonical problems, noindex, missing sitemap/robots.txt, slow pages, thin/duplicate content, missing HTTPS, missing structured data, missing viewport, and more); unmatched issues fall back to a generic rule so nothing is dropped. Each rule assigns a severity of **High / Medium / Low** (either forced by the rule or derived from the crawler's own severity) and a ready-to-read recommendation. Agent 4 produces the exact same structured result Agent 3 does - summary, severity breakdown, recommendations, priority actions, issue groups - so it renders through the same PDF report.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `BeyondSEO command not found` | set `beyondseo_cmd` in `config.json` (see step 2) |
| `Ollama not reachable` | start Ollama (`ollama serve` or the desktop app); check `ollama_host` |
| `Model 'qwen3:8b' is not installed` | `ollama pull qwen3:8b` |
| Crawl fails with exit code 1 | site unreachable, blocking bots, or disallowed by robots.txt; read `reports/<site>/crawl.log` |
| Crawl fails with exit code 2 | BeyondSEO setup problem; run its `doctor` command |
| Browser / Chromium errors | set `"crawl_mode": "http"` or run BeyondSEO's setup with `--http-only` |
| Crawling `localhost` / a staging server | add `"crawl_extra_args": ["--allow-private"]` |
| Crawl timed out | lower `crawl_max_pages` or raise `crawl_timeout_seconds` |
| Analysis is slow | use a smaller model (`qwen3:4b`), keep `ollama_think` off, or switch to Agent 4 (`--engine agent4`) |
| `At least one of 'enable_agent3' / 'enable_agent4' must be true` | set at least one of them to `true` in `config.json` |
| `--engine agentN was requested but 'enable_agentN' is false` | enable that agent in `config.json`, or drop `--engine` to use the enabled one |

## Notes and limitations

- BeyondSEO reports observations from the crawled sample only. It does not measure rankings, traffic, Core Web Vitals or indexing; the PDF lists these limits from BeyondSEO's own `summary.json`.
- Severity labels come from BeyondSEO. The LLM explains and prioritizes them but does not change them.
- The PDF uses ReportLab's built-in fonts, which cover Latin (Western European) text. Characters outside that range in page titles or evidence (for example Urdu or emoji) appear as `?` in the PDF; `issues.csv` keeps the original text.
- LLM output can be wrong. Treat recommendations as a starting point and check them against the evidence in `issues.csv`.
- Sites are processed one after another (crawls and local LLM inference are both heavy).

## Lead Discovery (Agent 5 - Google Maps browser scraper)

Find businesses first, then audit their websites:

    Keyword + Location -> Agent 5 (Playwright opens Google Maps) -> business name / address / phone / website
      -> you select businesses -> Agent 2 BeyondSEO crawl -> Agent 3/4 analysis -> Agent 4 PDF report

Open **Lead discovery** in the sidebar, enter a keyword (Dentists), a location (Lahore) and
Max results, click **Find Businesses**, tick the businesses you want and click **Run SEO Audit**.
Each selected website becomes a normal audit job (same engine, same History page, same reports).

**Setup (one time).** No Google API key, Google Cloud account or billing is needed:

    pip install -r requirements.txt
    playwright install chromium

Optional `config.json` settings: `"maps_headless": false` (default, you can watch the browser; `true`
hides it), `"maps_timeout_seconds": 30`, and `"maps_browser_channel": "chrome"` to use an installed
Google Chrome instead of Playwright's Chromium.

**What Agent 5 does.** Launches a browser, opens Google Maps, types "<keyword> in <location>",
scrolls the results list, then opens each business and reads its name, address, phone and website.
It removes duplicate businesses and duplicate websites, ignores businesses without a website, cleans
URLs (tracking parameters removed) and skips social-media/link-in-bio pages (facebook.com,
instagram.com, wa.me, ...) because they cannot be SEO-audited. Max results is limited to 60 per
search; each business is opened one by one, so a search takes roughly 2-4 seconds per business.

You can also run it without the web page: `python -m agents.agent5_google_maps "Dentists" "Lahore" --max 10`

**Keeping it working.** Google Maps has no public markup contract and changes its page now and then.
All selectors Agent 5 relies on are named constants in the `SELECTORS` block at the top of
`agents/agent5_google_maps.py`; if Google changes the page, that block is the only thing to edit.
If Google shows a CAPTCHA, Agent 5 stops with a clear message - wait a while and search fewer results.
Automated access to Google Maps is outside Google's terms of service, so use it sparingly and for
your own research.

**Data.** `data/leads.csv` holds every lead (`business_name, address, phone, website, audit_status,
audit_date` plus `job_id, keyword, location, discovered_at`). `data/last_discovery.csv` is the
latest search exactly as Agent 5 exported it (`business_name, address, phone, website`).
`data/lead_discoveries.json` is the search log used by the dashboard cards.

**Dashboard cards.** Total leads = unique businesses Agent 5 has read across all searches; With website =
rows in `leads.csv`; Audited websites = leads whose audit completed; Potential clients = audited
leads with at least one Critical or High issue; Latest discovery = the most recent search.

Reports for audits started from Lead Discovery show a **Business** block (name, address, phone,
website) at the top of the PDF and on the results page. Manual audits are unchanged.
