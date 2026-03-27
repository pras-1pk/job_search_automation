# 🤖 Job Search Automation Agent

> A fully automated, AI-powered job hunting agent that runs daily on Google Cloud — fetching fresh jobs, scoring them against your resume, and delivering ranked matches to your Telegram before you've had your morning chai.

---

## How It Works

```
Cloud Scheduler (9 AM IST daily)
         │
         ▼
    Cloud Run Job
         │
         ├─► Google Drive       → fetches your resume (PDF / DOCX / Google Doc)
         ├─► JSearch API        → pulls today's full-time jobs from LinkedIn, Indeed & more
         ├─► Local pre-ranker   → scores title, skills, seniority & experience fit
         ├─► Gemini 2.5 Flash   → ATS scores each shortlisted job vs your resume
         ├─► Google Sheets      → deduplicates + logs all results as a job tracker
         └─► Telegram Bot       → notifies you of matched jobs (score ≥ threshold)
```

No browser. No scrolling. Just a Telegram message every morning with the jobs worth applying for.

---

## Features

- **Zero LinkedIn scraping** — uses JSearch (RapidAPI) which aggregates LinkedIn, Indeed, Glassdoor, and more without hitting platform blocks
- **Two-layer deduplication** — in-memory dedup within a run + persistent dedup via Google Sheets across days, so you never see the same job twice
- **Local pre-ranker** — filters jobs by title keywords, skill overlap, seniority fit, and experience range *before* calling Gemini, saving API quota
- **AI ATS scoring** — Gemini 2.5 Flash-Lite scores each job 0–100 with a one-line explanation of why you match and what's missing
- **LinkedIn-first** — when a job appears on multiple platforms, the LinkedIn apply URL is always preferred
- **Google Sheet dashboard** — every job is logged with score, match reason, gaps, and a Status column (`Pending` / `Applied` / `Ignored`) you update manually
- **Secrets via Secret Manager** — no credentials in env vars or Docker images, ever
- **One-command deployment** — `cicd.sh` handles everything from API enablement to Cloud Scheduler in one run
- **Free tier** — runs entirely within GCP and Google AI Studio free limits (~$0.03/month)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Compute | Google Cloud Run Jobs |
| Scheduling | Google Cloud Scheduler |
| AI Scoring | Gemini 2.5 Flash-Lite (Google AI Studio) |
| Job Data | JSearch via RapidAPI |
| Resume Storage | Google Drive API |
| Job Tracker | Google Sheets API |
| Secrets | GCP Secret Manager |
| Notifications | Telegram Bot API |
| Container Registry | Google Artifact Registry |
| Language | Python 3.11 |

---

## Project Structure

```
job_search_automation/
├── main.py                      # Orchestrator — runs the full pipeline
├── config.py                    # Loads all secrets from GCP Secret Manager
├── requirements.txt
├── Dockerfile                   # Multi-stage, non-root, production-hardened
├── .dockerignore
├── cicd.sh                      # Full deployment script (infra → scheduler)
├── SETUP.md                     # Step-by-step manual setup guide
└── services/
    ├── drive_service.py         # Downloads and parses resume from Google Drive
    ├── jobs_service.py          # Fetches, filters, and pre-ranks jobs from JSearch
    ├── scoring_service.py       # Gemini ATS scoring with structured output + retries
    ├── sheets_service.py        # Dedup reads + job tracker writes to Google Sheets
    └── telegram_service.py      # Formats and sends Telegram notifications
```

---

## Prerequisites

- Google Cloud project with billing enabled
- [gcloud CLI](https://cloud.google.com/sdk/docs/install) installed and authenticated
- Docker installed and running
- Accounts needed (all have free tiers):
  - [Google AI Studio](https://aistudio.google.com) — Gemini API key
  - [RapidAPI JSearch](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) — job data
  - Telegram — create a bot via [@BotFather](https://t.me/botfather)

---

## Quick Start

### 1. Store secrets in GCP Secret Manager

```bash
# Run once for each secret
echo -n "your_value" | gcloud secrets create SECRET_NAME --data-file=-
```

Required secrets:

| Secret Name | Where to get it |
|---|---|
| `GOOGLE_SHEET_ID` | From your Google Sheet URL |
| `GOOGLE_DRIVE_RESUME_FILE_ID` | From your resume file URL on Drive |
| `JSEARCH_API_KEY` | RapidAPI dashboard |
| `GEMINI_API_KEY` | https://aistudio.google.com |
| `TELEGRAM_BOT_TOKEN` | @BotFather on Telegram |
| `TELEGRAM_CHAT_ID` | From `getUpdates` API call |

### 2. Share your Drive & Sheet with the service account

After running `cicd.sh`, share both with:
`job-agent-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com`

### 3. Deploy everything

```bash
# Clone the repo
git clone https://github.com/pras-1pk/job_search_automation.git
cd job_search_automation

# Full first-time deployment
bash cicd.sh
```

This single command handles: API enablement → service account → Artifact Registry → Docker build & push → Cloud Run Job → Cloud Scheduler at 9 AM IST.

### 4. Subsequent code updates

```bash
bash cicd.sh --update          # Rebuild image + redeploy
bash cicd.sh --health-check    # Verify everything is working
```

---

## Configuration

Non-sensitive config is passed as environment variables to Cloud Run (set inside `cicd.sh`):

| Variable | Default | Description |
|---|---|---|
| `JOB_SEARCH_QUERIES` | `Software Engineer 2,...` | Comma-separated search terms |
| `JOB_LOCATION` | `India` | Location filter passed to JSearch |
| `JOBS_PER_QUERY` | `10` | Max results per search query |
| `ATS_THRESHOLD` | `70` | Minimum ATS score to trigger Telegram alert |

---

## Google Sheet Dashboard

Every job scored (above or below threshold) is written to a **Jobs** tab automatically created on first run:

| Job ID | Title | Company | Location | Type | ATS Score | Why You Match | Missing Skills | Apply URL | Notified At | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| `abc123` | Backend Engineer | Razorpay | Bangalore | Full-time | 84% | Strong GCP + Python match | None | [LinkedIn](https://linkedin.com) | 2026-03-27 | **Pending** |

Update the **Status** column manually after reviewing each job: `Applied`, `Ignored`, or `Saved`.

---

## Cost Breakdown

| Service | Monthly usage | Cost |
|---|---|---|
| Cloud Run Job | 30 runs × ~3 min | **Free** |
| Cloud Scheduler | 1 job | **Free** |
| Secret Manager | ~180 reads/month | **Free** |
| JSearch API | ~90 requests/month | **Free** |
| Gemini 2.5 Flash-Lite | ~900 scoring calls/month | **Free** |
| Artifact Registry | ~140MB image storage | ~$0.03 |
| **Total** | | **~$0.03/month** |

---

## Telegram Notification Preview

```
🎯 5 new job matches found!
Sorted by ATS score — 🔵 LinkedIn jobs shown first

🔵 LinkedIn  |  📍 Bangalore
Backend Engineer II
🏢 Razorpay
🕐 Posted: 2026-03-27
📊 ATS: 84%  🟩🟩🟩🟩🟩🟩🟩🟩⬜⬜
✅ 4 years Python + GCP experience aligns directly with the role.
👉 Apply Here
```

---

## Local Development

```bash
# Authenticate with your personal Google account
gcloud auth application-default login

export GOOGLE_CLOUD_PROJECT=your-project-id

pip install -r requirements.txt
python main.py
```

The same auth code path works locally (ADC) and on Cloud Run (service account) — no changes needed between environments.

---

## License

MIT
