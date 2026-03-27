import json
import logging
import re
import time
from typing import Any
from typing_extensions import TypedDict

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

logger = logging.getLogger(__name__)

# ── Model ─────────────────────────────────────────────────────────────────────
MODEL = "gemini-2.5-flash-lite"

# ── Rate limit config ─────────────────────────────────────────────────────────
# Keep a safe buffer below free-tier limits.
SAFE_RPM = 12
SLEEP_BETWEEN_CALLS = 60 / SAFE_RPM  # 5 seconds

# ── Retry config ──────────────────────────────────────────────────────────────
MAX_RETRIES = 3
RETRY_BASE_DELAY = 30  # 30s → 60s → 120s on 429s

# ── Prompt limits ─────────────────────────────────────────────────────────────
RESUME_CHAR_LIMIT = 3000
JD_CHAR_LIMIT = 2500


class ATSResultSchema(TypedDict):
    ats_score: int
    why_match: str
    missing: str


def score_job(gemini_api_key: str, resume_text: str, job: dict) -> dict:
    """
    Score a job description against a resume using Gemini structured output.

    Main fixes:
      - Uses response_mime_type="application/json" + response_schema
      - Temperature set to 0.0 for deterministic scoring
      - Larger max_output_tokens to reduce truncation risk
      - Robust fallback parser for rare malformed outputs
      - Exponential backoff on rate limits
    """
    genai.configure(api_key=gemini_api_key)

    model = genai.GenerativeModel(
        model_name=MODEL,
        generation_config=genai.types.GenerationConfig(
            temperature=0.0,
            max_output_tokens=900,
            response_mime_type="application/json",
            response_schema=ATSResultSchema,
        ),
    )

    prompt = _build_prompt(resume_text, job)
    job_id = str(job.get("id", "unknown"))

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = model.generate_content(prompt)

            # Prefer SDK-parsed output if available.
            parsed = getattr(response, "parsed", None)
            if isinstance(parsed, dict):
                result = _normalize_result(parsed)
            else:
                raw_text = getattr(response, "text", "") or ""
                logger.debug("[RAW GEMINI OUTPUT] job=%s attempt=%s:\n%s", job_id, attempt, raw_text[:1500])
                result = _parse_response(raw_text, job_id)

            logger.debug(
                "Scored job %s — sleeping %.1fs (rate limiter)",
                job_id,
                SLEEP_BETWEEN_CALLS,
            )
            time.sleep(SLEEP_BETWEEN_CALLS)
            return result

        except ResourceExhausted:
            wait = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            if attempt < MAX_RETRIES:
                logger.warning(
                    "Gemini 429 on job %s (attempt %s/%s). Backing off %ss...",
                    job_id,
                    attempt,
                    MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
                continue

            logger.error("Gemini 429 on job %s — max retries hit.", job_id)
            return {"ats_score": 0, "why_match": "", "missing": "Rate limit hit"}

        except json.JSONDecodeError as e:
            logger.error("JSON parse error for job %s (attempt %s): %s", job_id, attempt, e)
            if attempt < MAX_RETRIES:
                time.sleep(5)
                continue
            return {"ats_score": 0, "why_match": "", "missing": "Scoring failed"}

        except Exception as e:
            logger.error("Scoring error for job %s (attempt %s): %s", job_id, attempt, e)
            if attempt < MAX_RETRIES:
                time.sleep(5)
                continue
            return {"ats_score": 0, "why_match": "", "missing": "Scoring failed"}

    return {"ats_score": 0, "why_match": "", "missing": "Scoring failed"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_prompt(resume_text: str, job: dict) -> str:
    return f"""You are an expert ATS (Applicant Tracking System) evaluator.

Compare the resume and the job description. Score the match objectively.

Rules:
- Output must match the schema exactly.
- Keep why_match concise: 1 to 2 sentences.
- Keep missing concise: mention the most important gaps, or "None" if there are no major gaps.
- Do not add markdown, code fences, bullets, or extra text.
- Do not mention that you are an AI.
- If the resume is weakly matched, still return valid JSON.

Resume:
{resume_text[:RESUME_CHAR_LIMIT]}

Job title:
{job.get("title", "")}

Company:
{job.get("company", "")}

Job description:
{job.get("description", "")[:JD_CHAR_LIMIT]}

Return JSON with exactly these fields:
{{
  "ats_score": 0,
  "why_match": "",
  "missing": ""
}}
"""


def _parse_response(raw_text: str, job_id: str) -> dict:
    """
    Robust JSON extraction with multiple fallback layers:
      1) Direct json.loads
      2) Strip markdown fences and try again
      3) Balanced JSON object extraction
      4) Field-level regex salvage
    """
    raw = (raw_text or "").strip()
    if not raw:
        raise json.JSONDecodeError("Empty model response", raw, 0)

    # 1) Direct parse
    try:
        return _normalize_result(json.loads(raw))
    except json.JSONDecodeError:
        pass

    # 2) Strip markdown fences
    if "```" in raw:
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
        if fence_match:
            fenced = fence_match.group(1).strip()
            try:
                return _normalize_result(json.loads(fenced))
            except json.JSONDecodeError:
                pass

    # 3) Balanced JSON extraction, safe for braces inside strings
    json_block = _extract_first_json_object(raw)
    if json_block:
        try:
            return _normalize_result(json.loads(json_block))
        except json.JSONDecodeError:
            pass

    # 4) Salvage individual fields
    score_match = re.search(r'"ats_score"\s*:\s*(-?\d+)', raw)
    why_match = re.search(r'"why_match"\s*:\s*"((?:\\.|[^"\\])*)"', raw, re.DOTALL)
    miss_match = re.search(r'"missing"\s*:\s*"((?:\\.|[^"\\])*)"', raw, re.DOTALL)

    if score_match:
        logger.warning(
            "Job %s: used regex field extraction because Gemini output was malformed.",
            job_id,
        )
        return {
            "ats_score": _clamp_score(int(score_match.group(1))),
            "why_match": _decode_json_string_fragment(why_match.group(1)) if why_match else "",
            "missing": _decode_json_string_fragment(miss_match.group(1)) if miss_match else "",
        }

    logger.error(
        "Job %s: all JSON extraction layers failed.\nRaw response (%s chars):\n%s",
        job_id,
        len(raw),
        raw[:1500],
    )
    raise json.JSONDecodeError("All extraction layers failed", raw, 0)


def _extract_first_json_object(text: str) -> str | None:
    """
    Return the first balanced JSON object found in text.
    Handles braces inside quoted strings and escaped characters.
    """
    start = None
    depth = 0
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if start is None:
            if ch == "{":
                start = i
                depth = 1
            continue

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None


def _normalize_result(result: dict) -> dict:
    """
    Ensure final output is always a clean dict with the expected types.
    """
    score = result.get("ats_score", 0)
    try:
        score = int(score)
    except (TypeError, ValueError):
        score = 0

    why_match = result.get("why_match", "")
    missing = result.get("missing", "")

    if why_match is None:
        why_match = ""
    if missing is None:
        missing = ""

    return {
        "ats_score": _clamp_score(score),
        "why_match": str(why_match).strip(),
        "missing": str(missing).strip(),
    }


def _decode_json_string_fragment(fragment: str) -> str:
    """
    Decode a JSON string fragment captured by regex.
    """
    try:
        return json.loads(f'"{fragment}"')
    except Exception:
        return fragment


def _clamp_score(score: int) -> int:
    return max(0, min(100, score))