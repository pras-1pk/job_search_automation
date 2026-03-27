import logging
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

import requests

logger = logging.getLogger(__name__)

JSEARCH_URL      = "https://jsearch.p.rapidapi.com/search"
LINKEDIN_LABEL   = "LinkedIn"

# JSearch date_posted values: today | 3days | week | month
# "today" = last 24h on their end, but their cutoff isn't always exact,
# so we also hard-filter by job_posted_at_timestamp after fetching.
DATE_POSTED      = "today"
MAX_AGE_HOURS    = 24


def fetch_jobs(
    api_key: str,
    queries: List[str],
    location: str,
    num_results: int = 10,
) -> List[Dict]:
    """
    Fetch full-time jobs posted in the last 24 hours from JSearch.

    Strategy:
      - Sends one API request per query keyword.
      - Filters: date_posted=today + employment_types=FULLTIME.
      - Hard-filters by timestamp post-fetch (jobs older than MAX_AGE_HOURS dropped).
      - Deduplicates across queries by job_id.
      - Sorts final list: LinkedIn-sourced jobs first, then by recency.
    """
    headers = {
        "X-RapidAPI-Key":  api_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }

    all_jobs: List[Dict] = []
    seen_ids: set        = set()
    cutoff_ts            = (datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)).timestamp()

    for query in queries:
        # Embed location directly in the query string — JSearch docs recommend this
        search_query = f"{query} in {location}"
        params = {
            "query":            search_query,
            "page":             "1",
            "num_pages":        "1",
            "date_posted":      DATE_POSTED,      # Pre-filter: last 24h
            "employment_types": "FULLTIME",        # Only full-time roles
            "remote_jobs_only": "false",
        }

        try:
            resp = requests.get(JSEARCH_URL, headers=headers, params=params, timeout=30)

            # Surface rate-limit info so we know when we're close to the 200/month cap
            remaining = resp.headers.get("X-RateLimit-Requests-Remaining")
            if remaining:
                logger.info(f"JSearch quota remaining: {remaining} requests")

            resp.raise_for_status()
            data = resp.json()

            jobs_raw = data.get("data", [])
            logger.info(f"Query '{search_query}': API returned {len(jobs_raw)} jobs")

            accepted = 0
            for raw in jobs_raw:
                job_id = raw.get("job_id")
                if not job_id or job_id in seen_ids:
                    continue

                # Hard timestamp filter — drop anything older than 24h
                posted_ts = raw.get("job_posted_at_timestamp")
                if posted_ts and float(posted_ts) < cutoff_ts:
                    logger.debug(f"Skipping old job {job_id} (posted {_age_label(posted_ts)})")
                    continue

                seen_ids.add(job_id)
                all_jobs.append(_normalise(raw))
                accepted += 1

                if accepted >= num_results:
                    break

            logger.info(f"Query '{search_query}': {accepted} jobs accepted after 24h filter")

        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP {resp.status_code} for query '{query}': {resp.text[:300]}")
        except Exception as e:
            logger.error(f"Unexpected error for query '{query}': {e}")

    # Sort: LinkedIn jobs first, then by posted timestamp descending (newest first)
    all_jobs.sort(key=_sort_key, reverse=True)

    linkedin_count = sum(1 for j in all_jobs if j["is_linkedin"])
    logger.info(
        f"Total unique jobs: {len(all_jobs)} "
        f"({linkedin_count} from LinkedIn, {len(all_jobs) - linkedin_count} from other sources)"
    )
    return all_jobs


# ── Private helpers ──────────────────────────────────────────────────────────

def _normalise(raw: dict) -> dict:
    """
    Map raw JSearch fields to our internal schema.

    LinkedIn priority logic:
      1. job_publisher == "LinkedIn"  → primary source is LinkedIn
      2. apply_options contains a LinkedIn entry → job is on LinkedIn even if
         primary publisher is something else (e.g. job board aggregator)
      In both cases we use the LinkedIn apply URL so the user lands directly
      on the LinkedIn job page.
    """
    publisher     = raw.get("job_publisher", "")
    apply_options = raw.get("apply_options", [])

    # Find the LinkedIn apply option if it exists
    linkedin_option: Optional[dict] = next(
        (opt for opt in apply_options if opt.get("publisher", "").lower() == "linkedin"),
        None
    )

    is_linkedin = (publisher == LINKEDIN_LABEL) or (linkedin_option is not None)

    # Prefer LinkedIn URL; fall back to primary apply link, then Google link
    if linkedin_option:
        url = linkedin_option["apply_link"]
    else:
        url = raw.get("job_apply_link") or raw.get("job_google_link", "")

    posted_ts = raw.get("job_posted_at_timestamp")

    return {
        "id":              raw.get("job_id", ""),
        "title":           raw.get("job_title", ""),
        "company":         raw.get("employer_name", ""),
        "location":        (
            raw.get("job_city")
            or raw.get("job_state")
            or raw.get("job_country", "")
        ),
        "description":     raw.get("job_description", ""),
        "url":             url,
        "employment_type": raw.get("job_employment_type", ""),
        "remote":          raw.get("job_is_remote", False),
        "publisher":       publisher,
        "is_linkedin":     is_linkedin,
        "posted_ts":       float(posted_ts) if posted_ts else 0.0,
        "posted_at":       raw.get("job_posted_at_datetime_utc", ""),
        "salary_min":      raw.get("job_min_salary"),
        "salary_max":      raw.get("job_max_salary"),
        "salary_currency": raw.get("job_salary_currency", ""),
    }


def _sort_key(job: dict) -> tuple:
    """
    Sort tuple: (is_linkedin, posted_ts)
    Both descending → LinkedIn jobs first, then newest first within each group.
    """
    return (int(job["is_linkedin"]), job["posted_ts"])


def _age_label(ts) -> str:
    """Human-readable age string for debug logs."""
    age_mins = (time.time() - float(ts)) / 60
    if age_mins < 60:
        return f"{int(age_mins)}m ago"
    return f"{age_mins / 60:.1f}h ago"