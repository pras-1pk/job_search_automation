import logging
import sys
from config import (
    GOOGLE_SHEET_ID, GOOGLE_DRIVE_RESUME_FILE_ID,
    JSEARCH_API_KEY, GEMINI_API_KEY,
    JOB_SEARCH_QUERIES, JOB_LOCATION, JOBS_PER_QUERY,
    ATS_THRESHOLD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
)
from services.drive_service import fetch_resume_text
from services.sheets_service import get_seen_job_ids, append_jobs
from services.jobs_service import fetch_jobs
from services.scoring_service import score_job
from services.telegram_service import send_jobs_notification

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)


def main():
    logger.info("=== Job Agent Starting ===")

    # 1. Fetch resume from Google Drive
    logger.info("Fetching resume from Google Drive...")
    resume_text = fetch_resume_text(GOOGLE_DRIVE_RESUME_FILE_ID)
    logger.info(f"Resume extracted: {len(resume_text)} characters")

    # 2. Load seen job IDs from Google Sheets
    logger.info("Loading seen job IDs from Google Sheets...")
    seen_ids = get_seen_job_ids(GOOGLE_SHEET_ID)
    logger.info(f"Already seen: {len(seen_ids)} jobs")

    # 3. Fetch fresh jobs from JSearch
    logger.info(f"Fetching jobs for queries: {JOB_SEARCH_QUERIES}")
    all_jobs = fetch_jobs(JSEARCH_API_KEY, JOB_SEARCH_QUERIES, JOB_LOCATION, JOBS_PER_QUERY)
    logger.info(f"Total jobs fetched: {len(all_jobs)}")

    # 4. Filter out already seen
    new_jobs = [j for j in all_jobs if j["id"] not in seen_ids]
    logger.info(f"New (unseen) jobs to process: {len(new_jobs)}")

    if not new_jobs:
        logger.info("No new jobs found. Sending summary notification.")
        send_jobs_notification(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, [])
        return

    # 5. Score each job with Gemini 2.5 Flash
    # Each call sleeps 7.5s (rate limiter) → ~8 RPM, safely under free tier 10 RPM cap
    est_mins = len(new_jobs) * 7.5 / 60
    logger.info(f"Scoring {len(new_jobs)} jobs with Gemini 2.5 Flash (~{est_mins:.1f} min estimated)...")

    scored_jobs = []
    for idx, job in enumerate(new_jobs, 1):
        logger.info(f"  Scoring {idx}/{len(new_jobs)}: {job['title']} @ {job['company']}")
        scores = score_job(GEMINI_API_KEY, resume_text, job)
        job.update(scores)
        scored_jobs.append(job)
        logger.info(f"  [{job['ats_score']}%] {job['title']} @ {job['company']}")

    # 6. Filter by ATS threshold
    matched_jobs = sorted(
        [j for j in scored_jobs if j["ats_score"] >= ATS_THRESHOLD],
        key=lambda x: x["ats_score"],
        reverse=True
    )
    logger.info(f"Jobs above {ATS_THRESHOLD}% threshold: {len(matched_jobs)}")

    # 7. Write ALL scored jobs to sheet (full audit trail)
    append_jobs(GOOGLE_SHEET_ID, scored_jobs)

    # 8. Notify on Telegram (only matched jobs)
    send_jobs_notification(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, matched_jobs)

    logger.info(f"=== Done. Notified about {len(matched_jobs)} matched jobs ===")


if __name__ == "__main__":
    main()