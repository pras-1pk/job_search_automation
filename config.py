import os
import logging
from google.cloud import secretmanager

logger = logging.getLogger(__name__)


def _get_secret(project_id: str, secret_id: str) -> str:
    """
    Fetch the latest version of a secret from GCP Secret Manager.
    Works automatically on Cloud Run (uses attached service account).
    Works locally after: gcloud auth application-default login
    """
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8").strip()


# ── Detect GCP Project ID ────────────────────────────────────────────────────
# On Cloud Run this env var is auto-populated.
# Locally set it: export GOOGLE_CLOUD_PROJECT=your-project-id
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT")
if not PROJECT_ID:
    raise EnvironmentError(
        "Could not determine GCP Project ID. "
        "Set GOOGLE_CLOUD_PROJECT environment variable."
    )

logger.info(f"Loading secrets from project: {PROJECT_ID}")

# ── Secrets (pulled from Secret Manager) ─────────────────────────────────────
GOOGLE_SHEET_ID             = _get_secret(PROJECT_ID, "GOOGLE_SHEET_ID")
GOOGLE_DRIVE_RESUME_FILE_ID = _get_secret(PROJECT_ID, "GOOGLE_DRIVE_RESUME_FILE_ID")
JSEARCH_API_KEY             = _get_secret(PROJECT_ID, "JSEARCH_API_KEY")
GEMINI_API_KEY              = _get_secret(PROJECT_ID, "gemini_api_key")
TELEGRAM_BOT_TOKEN          = _get_secret(PROJECT_ID, "TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID            = _get_secret(PROJECT_ID, "TELEGRAM_CHAT_ID")

# ── Non-sensitive config (plain env vars with sane defaults) ──────────────────
JOB_SEARCH_QUERIES = [
    q.strip()
    for q in os.environ.get(
        "JOB_SEARCH_QUERIES",
        "Software Engineer 2,Backend Developer,Senior Software Developer"
    ).split(",")
]
JOB_LOCATION   = os.environ.get("JOB_LOCATION", "India")
JOBS_PER_QUERY = int(os.environ.get("JOBS_PER_QUERY", "10"))
ATS_THRESHOLD  = int(os.environ.get("ATS_THRESHOLD", "70"))