# Job Agent — Setup Guide

## Architecture

```
Cloud Scheduler (daily 9 AM IST)
         │  HTTP POST
         ▼
    Cloud Run Job
         │
         ├─► Secret Manager        → all API keys & IDs
         ├─► Google Drive API      → fetch resume (PDF/DOCX/Google Doc)
         ├─► JSearch RapidAPI      → fetch today's jobs
         ├─► Google Sheets API     → read seen IDs (dedup)
         ├─► Gemini 2.0 Flash API  → ATS score each job (free tier)
         ├─► Google Sheets API     → write all scored jobs
         └─► Telegram Bot API      → notify matched jobs
```

---

## Secrets already in Secret Manager

| Secret Name                   | What it is                           |
|-------------------------------|--------------------------------------|
| `GOOGLE_SHEET_ID`             | ID from your Google Sheet URL        |
| `GOOGLE_DRIVE_RESUME_FILE_ID` | ID from your resume file URL         |
| `JSEARCH_API_KEY`             | RapidAPI key from jsearch            |
| `GEMINI_API_KEY`              | From https://aistudio.google.com     |
| `TELEGRAM_BOT_TOKEN`          | From @BotFather on Telegram          |
| `TELEGRAM_CHAT_ID`            | From getUpdates API call             |

Verify all 6:
```bash
gcloud secrets list
```

---

## Step 1 — Set your shell variables

```bash
export PROJECT_ID=$(gcloud config get-value project)
export REGION=asia-south1
export SA_EMAIL="job-agent-sa@${PROJECT_ID}.iam.gserviceaccount.com"

echo "Project : $PROJECT_ID"
echo "Region  : $REGION"
echo "SA      : $SA_EMAIL"
```

---

## Step 2 — Enable required GCP APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  drive.googleapis.com \
  sheets.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com
```

---

## Step 3 — Create the Service Account

```bash
gcloud iam service-accounts create job-agent-sa \
  --display-name="Job Agent Service Account"

# Read secrets
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"

# Invoke Cloud Run jobs (needed by Scheduler)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker"
```

---

## Step 4 — Share Google Sheet with service account

1. Open your Google Sheet
2. Click **Share** → add `job-agent-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com`
3. Role: **Editor** → Done

The agent auto-creates a "Jobs" tab with formatted headers on first run.

---

## Step 5 — Share resume file with service account

1. Open your resume in Google Drive
2. Click **Share** → add same service account email
3. Role: **Viewer** → Done

---

## Step 6 — Build and push Docker image

```bash
# Create Artifact Registry repo (one-time)
gcloud artifacts repositories create job-agent \
  --repository-format=docker \
  --location=$REGION

# Auth Docker
gcloud auth configure-docker ${REGION}-docker.pkg.dev

# Build and push (run from job-agent/ folder)
cd job-agent/
docker build -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/job-agent/job-agent:latest .
docker push ${REGION}-docker.pkg.dev/${PROJECT_ID}/job-agent/job-agent:latest
```

---

## Step 7 — Deploy Cloud Run Job

```bash
gcloud run jobs create job-agent \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/job-agent/job-agent:latest" \
  --region=$REGION \
  --service-account="${SA_EMAIL}" \
  --max-retries=1 \
  --task-timeout=300s \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID}" \
  --set-env-vars="JOB_SEARCH_QUERIES=backend engineer,python developer,software engineer" \
  --set-env-vars="JOB_LOCATION=India" \
  --set-env-vars="JOBS_PER_QUERY=10" \
  --set-env-vars="ATS_THRESHOLD=70"
```

No `--set-secrets` needed — Python fetches them directly from Secret Manager
using the service account identity attached to the job.

---

## Step 8 — Test run manually

```bash
gcloud run jobs execute job-agent --region=$REGION --wait

# View logs
gcloud logging read \
  "resource.type=cloud_run_job AND resource.labels.job_name=job-agent" \
  --limit=50 --format="table(timestamp, textPayload)" --freshness=10m
```

Expected log sequence:
```
Loading secrets from project: your-project   ← Secret Manager ✅
Resume extracted: 2847 characters             ← Drive ✅
Total jobs fetched: 28                        ← JSearch ✅
[84%] Backend Engineer @ Razorpay            ← Gemini scoring ✅
Appended 28 jobs to Google Sheet             ← Sheets ✅
Notified about 5 matched jobs               ← Telegram ✅
```

---

## Step 9 — Schedule with Cloud Scheduler

```bash
# Every day at 9:00 AM IST (03:30 UTC)
gcloud scheduler jobs create http job-agent-daily \
  --location=$REGION \
  --schedule="30 3 * * *" \
  --time-zone="Asia/Kolkata" \
  --uri="https://${REGION}-run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/job-agent:run" \
  --http-method=POST \
  --oauth-service-account-email="${SA_EMAIL}"

# Verify
gcloud scheduler jobs list --location=$REGION

# Trigger immediately to test
gcloud scheduler jobs run job-agent-daily --location=$REGION
```

---

## Local Development

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=your_project_id

pip install -r requirements.txt
python main.py
```

The same code runs locally (your personal credentials) and on Cloud Run
(service account) — no changes needed between environments.

---

## Deploying updates

```bash
docker build -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/job-agent/job-agent:latest .
docker push ${REGION}-docker.pkg.dev/${PROJECT_ID}/job-agent/job-agent:latest

gcloud run jobs update job-agent \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/job-agent/job-agent:latest" \
  --region=$REGION
```

---

## Monthly Cost

| Service           | Cost       |
|-------------------|------------|
| Cloud Run Job     | Free       |
| Cloud Scheduler   | Free       |
| Secret Manager    | Free       |
| JSearch API       | Free       |
| Gemini 2.0 Flash  | Free       |
| Artifact Registry | ~$0.03     |
| **Total**         | **~$0.03** |

---

## Troubleshooting

**Secret not found**
→ `gcloud secrets list` — names are case-sensitive, must match exactly

**Permission denied on secrets**
→ Service account missing `secretmanager.secretAccessor` role (Step 3)

**Empty resume text**
→ Resume not shared with service account (Step 5)
→ Scanned PDF (image-only) won't work — use text-based PDF

**Sheet not updating / gspread AuthError**
→ Sheet not shared with service account as Editor (Step 4)

**No Telegram messages**
→ Send your bot a message first — bots can't initiate conversations
→ Verify chat ID: `curl https://api.telegram.org/bot<TOKEN>/getUpdates`

**0 jobs from JSearch**
→ Check RapidAPI dashboard — 200 req/month free limit
→ `date_posted=today` returns fewer results on weekends
