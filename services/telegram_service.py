import logging

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_jobs_notification(bot_token: str, chat_id: str, jobs: list):
    """
    Send matched jobs to Telegram.
    - No matches  → single "all clear" message
    - Has matches → header + one message per job (sorted by score desc)
    """
    if not jobs:
        _send(bot_token, chat_id, "🤖 *Job Agent ran* — no new matches above threshold today\\.")
        return

    # Header
    _send(bot_token, chat_id,
          f"🎯 *{len(jobs)} new job match{'es' if len(jobs) > 1 else ''} found\\!*\n"
          f"_Sorted by ATS score — 🔵 LinkedIn jobs shown first_")

    for job in jobs:
        _send(bot_token, chat_id, _format_job(job))

    # Footer with sheet reminder
    _send(bot_token, chat_id, "📊 All jobs logged in your Google Sheet\\. Update *Status* column after reviewing\\.")


def _format_job(job: dict) -> str:
    score = job.get("ats_score", 0)
    score_bar = _score_bar(score)

    location_tag = "🌐 Remote" if job.get("remote") else f"📍 {_escape(job.get('location', 'N/A'))}"

    # LinkedIn badge
    source_tag = "🔵 *LinkedIn*" if job.get("is_linkedin") else f"🔗 {_escape(job.get('publisher', 'Other'))}"

    salary_str = ""
    if job.get("salary_min") and job.get("salary_max"):
        curr = job.get("salary_currency", "")
        salary_str = f"\n💰 {_escape(curr)} {job['salary_min']:,} – {job['salary_max']:,}"

    missing = job.get("missing", "")
    missing_str = f"\n⚠️ _Missing: {_escape(missing)}_" if missing and missing.lower() != "none" else ""

    # Posted age
    posted_at = job.get("posted_at", "")
    posted_str = f"\n🕐 Posted: {_escape(posted_at[:10])}" if posted_at else ""

    url = job.get("url", "")

    return (
        f"{source_tag}  \\|  {location_tag}\n"
        f"*{_escape(job.get('title', ''))}*\n"
        f"🏢 {_escape(job.get('company', ''))}"
        f"{posted_str}"
        f"{salary_str}\n"
        f"📊 ATS: *{score}%*  {score_bar}\n"
        f"✅ {_escape(job.get('why_match', ''))}"
        f"{missing_str}\n"
        f"👉 [Apply Here]({url})"
    )


def _score_bar(score: int) -> str:
    """Visual score bar using emoji blocks."""
    filled = round(score / 10)
    return "🟩" * filled + "⬜" * (10 - filled)


def _escape(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


def _send(bot_token: str, chat_id: str, text: str):
    url = TELEGRAM_API.format(token=bot_token)
    payload = {
        "chat_id":                  chat_id,
        "text":                     text,
        "parse_mode":               "MarkdownV2",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            logger.error(f"Telegram error {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")