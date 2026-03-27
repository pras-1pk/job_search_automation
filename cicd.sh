#!/usr/bin/env bash
# =============================================================================
# cicd.sh — Job Agent full deployment pipeline
#
# Covers: API enablement → Service Account → Artifact Registry →
#         Docker build → Docker push → Artifact cleanup → Cloud Run Job →
#         Cloud Scheduler → Post-deployment health check
#
# Usage:
#   First deploy  :  bash cicd.sh
#   Code update   :  bash cicd.sh --update
#   Skip docker   :  bash cicd.sh --skip-docker
#   Health check  :  bash cicd.sh --health-check
#
# Requirements:
#   - .env file present (copy .env.example → .env and fill in values)
#   - gcloud CLI installed and authenticated (gcloud auth login)
#   - docker installed and running
#   - All 6 secrets already present in Secret Manager
# =============================================================================

set -euo pipefail   # Exit on error, undefined var, or pipe failure

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; exit 1; }
section() { echo -e "\n${BOLD}━━━ $* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"; }

# ── Load .env ─────────────────────────────────────────────────────────────────
ENV_FILE="$(dirname "$0")/.env"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  info "Loaded config from .env"
else
  error ".env file not found.\nCopy the template and fill in your values:\n  cp .env.example .env"
fi

# ── Parse flags ───────────────────────────────────────────────────────────────
UPDATE=false
SKIP_DOCKER=false
HEALTH_CHECK_ONLY=false

for arg in "$@"; do
  case $arg in
    --update)         UPDATE=true ;;
    --skip-docker)    SKIP_DOCKER=true ;;
    --health-check)   HEALTH_CHECK_ONLY=true ;;
    --help)
      echo "Usage: bash cicd.sh [--update] [--skip-docker] [--health-check]"
      echo "  --update        Skip infra setup, only rebuild image + update Cloud Run job"
      echo "  --skip-docker   Skip docker build/push (use last pushed image)"
      echo "  --health-check  Only run post-deployment checks, skip all deployment steps"
      exit 0
      ;;
    *) warn "Unknown flag: $arg (ignored)" ;;
  esac
done

# ── Config ────────────────────────────────────────────────────────────────────
# Project ID: prefer .env value, fall back to gcloud config
if [[ -n "${GCP_PROJECT_ID:-}" ]]; then
  PROJECT_ID="$GCP_PROJECT_ID"
else
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
fi
[[ -z "$PROJECT_ID" ]] && error "No GCP project set.\nEither set GCP_PROJECT_ID in .env or run: gcloud config set project YOUR_PROJECT_ID"

# All other config comes from .env — validate required vars are present
REQUIRED_ENV_VARS=(
  GCP_REGION JOB_NAME SA_NAME REPO_NAME SCHEDULER_NAME
  SCHEDULE TIMEZONE
  JOB_SEARCH_QUERIES JOB_LOCATION JOBS_PER_QUERY ATS_THRESHOLD
)
MISSING_ENV=()
for var in "${REQUIRED_ENV_VARS[@]}"; do
  [[ -z "${!var:-}" ]] && MISSING_ENV+=("$var")
done
if [[ ${#MISSING_ENV[@]} -gt 0 ]]; then
  error "Missing required variables in .env: ${MISSING_ENV[*]}\nCheck .env.example for reference."
fi

# Derived values (never hardcoded)
REGION="$GCP_REGION"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${JOB_NAME}:latest"

# ── Health check only mode ────────────────────────────────────────────────────
if [[ "$HEALTH_CHECK_ONLY" == "true" ]]; then
  section "Health Check Mode — skipping all deployment steps"
fi

echo -e "\n${BOLD}╔══════════════════════════════════════════╗"
echo -e "║        Job Agent — CI/CD Pipeline        ║"
echo -e "╚══════════════════════════════════════════╝${RESET}"
info "Project  : ${PROJECT_ID}"
info "Region   : ${REGION}"
info "Timezone : ${TIMEZONE}"
info "Schedule : ${SCHEDULE} (${TIMEZONE})"
info "Image    : ${IMAGE}"
echo ""

if [[ "$HEALTH_CHECK_ONLY" != "true" ]]; then

  # ── Verify secrets exist before doing anything ──────────────────────────────
  section "Pre-flight: Verifying secrets in Secret Manager"

  REQUIRED_SECRETS=(
    "GOOGLE_SHEET_ID"
    "GOOGLE_DRIVE_RESUME_FILE_ID"
    "JSEARCH_API_KEY"
    "GEMINI_API_KEY"
    "TELEGRAM_BOT_TOKEN"
    "TELEGRAM_CHAT_ID"
  )

  MISSING_SECRETS=()
  for secret in "${REQUIRED_SECRETS[@]}"; do
    if gcloud secrets describe "$secret" --project="$PROJECT_ID" &>/dev/null; then
      success "Secret found: $secret"
    else
      MISSING_SECRETS+=("$secret")
      warn "Secret MISSING: $secret"
    fi
  done

  if [[ ${#MISSING_SECRETS[@]} -gt 0 ]]; then
    error "Missing secrets: ${MISSING_SECRETS[*]}\nCreate them first:\n  echo -n 'your_value' | gcloud secrets create SECRET_NAME --data-file=-"
  fi

  # ── Skip infra steps on --update ───────────────────────────────────────────
  if [[ "$UPDATE" == "true" ]]; then
    warn "--update flag set: skipping infra setup (APIs / SA / Scheduler). Jumping to Docker build."
  else

    # ── Step 1: Enable APIs ───────────────────────────────────────────────────
    section "Step 1/8 — Enabling GCP APIs"

    APIS=(
      "run.googleapis.com"
      "cloudscheduler.googleapis.com"
      "drive.googleapis.com"
      "sheets.googleapis.com"
      "secretmanager.googleapis.com"
      "artifactregistry.googleapis.com"
      "cloudbuild.googleapis.com"
    )

    for api in "${APIS[@]}"; do
      if gcloud services list --enabled --filter="name:${api}" --format="value(name)" 2>/dev/null | grep -q "$api"; then
        info "Already enabled: $api"
      else
        info "Enabling: $api"
        gcloud services enable "$api" --project="$PROJECT_ID"
        success "Enabled: $api"
      fi
    done

    # ── Step 2: Service Account ───────────────────────────────────────────────
    section "Step 2/8 — Service Account"

    if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" &>/dev/null; then
      info "Service account already exists: $SA_EMAIL"
    else
      info "Creating service account..."
      gcloud iam service-accounts create "$SA_NAME" \
        --display-name="Job Agent Service Account" \
        --project="$PROJECT_ID"
      success "Created: $SA_EMAIL"
    fi

    # Grant roles (idempotent — safe to re-run)
    for role in "roles/secretmanager.secretAccessor" "roles/run.invoker"; do
      info "Granting $role to service account..."
      gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="$role" \
        --condition=None \
        --quiet
      success "Granted: $role"
    done

    # ── Step 3: Artifact Registry ─────────────────────────────────────────────
    section "Step 3/8 — Artifact Registry"

    if gcloud artifacts repositories describe "$REPO_NAME" \
         --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
      info "Repository already exists: $REPO_NAME"
    else
      info "Creating Artifact Registry repository..."
      gcloud artifacts repositories create "$REPO_NAME" \
        --repository-format=docker \
        --location="$REGION" \
        --description="Job agent Docker images" \
        --project="$PROJECT_ID"
      success "Repository created: $REPO_NAME"
    fi

    info "Configuring Docker auth for ${REGION}-docker.pkg.dev..."
    gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
    success "Docker auth configured"

  fi  # end of infra block

  # ── Step 4: Build & Push via Cloud Build ──────────────────────────────────────
  section "Step 4/8 — Building with Cloud Build"

  if [[ "$SKIP_DOCKER" == "true" ]]; then
    warn "--skip-docker flag set: using last pushed image (${IMAGE})"
  else
    info "Submitting build to Google Cloud Build..."
    gcloud builds submit --tag "$IMAGE" --project="$PROJECT_ID" .
    success "Image built and pushed successfully: $IMAGE"
  fi

  # ── Step 5: Artifact Registry cleanup (keep only latest) ─────────────────────
  section "Step 5/8 — Artifact Registry Cleanup"
  # Keeps only the :latest tag. Deletes all untagged/old digests.
  # This keeps you permanently inside the 0.5GB free tier.

  info "Listing all image digests in registry..."

  ALL_DIGESTS=$(gcloud artifacts docker images list \
    "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${JOB_NAME}" \
    --include-tags \
    --format="value(version)" \
    --project="$PROJECT_ID" 2>/dev/null || true)

  DIGEST_COUNT=$(echo "$ALL_DIGESTS" | grep -c "sha256" || true)
  info "Found ${DIGEST_COUNT} image digest(s) in registry"

  if [[ "$DIGEST_COUNT" -le 1 ]]; then
    success "Only 1 image present — nothing to clean up"
  else
    LATEST_DIGEST=$(gcloud artifacts docker images describe \
      "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${JOB_NAME}:latest" \
      --format="value(image_summary.digest)" \
      --project="$PROJECT_ID" 2>/dev/null || true)

    info "Keeping latest digest: ${LATEST_DIGEST:0:19}..."

    DELETED=0
    while IFS= read -r digest; do
      [[ -z "$digest" ]] && continue
      if [[ "$digest" == "$LATEST_DIGEST" ]]; then
        continue
      fi
      info "Deleting old image: ${digest:0:19}..."
      gcloud artifacts docker images delete \
        "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${JOB_NAME}@${digest}" \
        --quiet \
        --project="$PROJECT_ID" 2>/dev/null && \
        DELETED=$((DELETED + 1)) || \
        warn "Could not delete ${digest:0:19} (may already be gone)"
    done <<< "$ALL_DIGESTS"

    success "Cleaned up ${DELETED} old image(s) — registry now holds only :latest"
  fi

  info "Current Artifact Registry usage:"
  gcloud artifacts docker images list \
    "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${JOB_NAME}" \
    --include-tags \
    --format="table(version.slice(0:19), tags, createTime)" \
    --project="$PROJECT_ID" 2>/dev/null || true

  # ── Step 6: Cloud Run Job ────────────────────────────────────────────────────
  section "Step 6/8 — Cloud Run Job"

  # Build env_vars.yaml from .env values — no hardcoding here
  cat > env_vars.yaml <<EOF
  GOOGLE_CLOUD_PROJECT: "${PROJECT_ID}"
  JOB_SEARCH_QUERIES: "${JOB_SEARCH_QUERIES}"
  JOB_LOCATION: "${JOB_LOCATION}"
  JOBS_PER_QUERY: "${JOBS_PER_QUERY}"
  ATS_THRESHOLD: "${ATS_THRESHOLD}"
  EOF

  if gcloud run jobs describe "$JOB_NAME" --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
    info "Cloud Run job exists — updating..."
    gcloud run jobs update "$JOB_NAME" \
      --image="${IMAGE}" \
      --region="${REGION}" \
      --service-account="${SA_EMAIL}" \
      --env-vars-file=env_vars.yaml \
      --project="$PROJECT_ID"
  else
    info "Creating Cloud Run job..."
    gcloud run jobs create "$JOB_NAME" \
      --image="${IMAGE}" \
      --region="${REGION}" \
      --service-account="${SA_EMAIL}" \
      --env-vars-file=env_vars.yaml \
      --project="$PROJECT_ID"
  fi

  rm env_vars.yaml

  # ── Step 7: Cloud Scheduler ───────────────────────────────────────────────────
  section "Step 7/8 — Cloud Scheduler"

  JOB_URI="https://${REGION}-run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB_NAME}:run"

  if gcloud scheduler jobs describe "$SCHEDULER_NAME" \
       --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
    info "Scheduler job already exists — updating..."
    gcloud scheduler jobs update http "$SCHEDULER_NAME" \
      --location="$REGION" \
      --schedule="$SCHEDULE" \
      --time-zone="$TIMEZONE" \
      --uri="$JOB_URI" \
      --http-method=POST \
      --oauth-service-account-email="$SA_EMAIL" \
      --project="$PROJECT_ID"
    success "Scheduler updated: ${SCHEDULE} (${TIMEZONE})"
  else
    info "Creating Cloud Scheduler job..."
    gcloud scheduler jobs create http "$SCHEDULER_NAME" \
      --location="$REGION" \
      --schedule="$SCHEDULE" \
      --time-zone="$TIMEZONE" \
      --uri="$JOB_URI" \
      --http-method=POST \
      --oauth-service-account-email="$SA_EMAIL" \
      --description="Triggers Job Agent — ${SCHEDULE} (${TIMEZONE})" \
      --project="$PROJECT_ID"
    success "Scheduler created: ${SCHEDULE} (${TIMEZONE})"
  fi

  # ── Step 8: Smoke test ────────────────────────────────────────────────────────
  section "Step 8/8 — Smoke Test (optional)"

  read -rp "$(echo -e "${YELLOW}Run a test execution now? [y/N]: ${RESET}")" RUN_TEST
  if [[ "$RUN_TEST" =~ ^[Yy]$ ]]; then
    info "Triggering job and waiting for completion (up to 10 min)..."
    gcloud run jobs execute "$JOB_NAME" \
      --region="$REGION" \
      --project="$PROJECT_ID" \
      --wait
    success "Test execution complete"
  else
    info "Skipping smoke test — you can run it later with:"
    info "  gcloud run jobs execute $JOB_NAME --region=$REGION --wait"
  fi

fi  # end of deployment block

# ═════════════════════════════════════════════════════════════════════════════
# POST-DEPLOYMENT HEALTH CHECK
# Run anytime with: bash cicd.sh --health-check
# Also runs automatically after every full deployment
# ═════════════════════════════════════════════════════════════════════════════
section "Post-Deployment Health Check"

PASS=0; FAIL=0

check_pass() { echo -e "  ${GREEN}✔${RESET}  $*"; PASS=$((PASS+1)); }
check_fail() { echo -e "  ${RED}✘${RESET}  $*"; FAIL=$((FAIL+1)); }
check_warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }

echo ""
echo -e "${BOLD}1. Cloud Run Job${RESET}"
JOB_STATUS=$(gcloud run jobs describe "$JOB_NAME" \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --format="value(status)" 2>/dev/null || echo "NOT_FOUND")

if [[ "$JOB_STATUS" == "NOT_FOUND" ]]; then
  check_fail "Cloud Run job '$JOB_NAME' not found in $REGION"
else
  check_pass "Cloud Run job '$JOB_NAME' exists in $REGION"

  LAST_EXEC=$(gcloud run jobs executions list \
    --job="$JOB_NAME" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --limit=1 \
    --format="value(status.conditions[0].type, status.conditions[0].status, completionTime)" \
    2>/dev/null | head -1 || true)

  if [[ -z "$LAST_EXEC" ]]; then
    check_warn "No executions found yet — job has never run"
  else
    EXEC_TYPE=$(echo "$LAST_EXEC"   | awk '{print $1}')
    EXEC_STATUS=$(echo "$LAST_EXEC" | awk '{print $2}')
    EXEC_TIME=$(echo "$LAST_EXEC"   | awk '{print $3}')

    if [[ "$EXEC_TYPE" == "Completed" && "$EXEC_STATUS" == "True" ]]; then
      check_pass "Last execution: SUCCEEDED at $EXEC_TIME"
    elif [[ "$EXEC_TYPE" == "Failed" ]]; then
      check_fail "Last execution: FAILED at $EXEC_TIME — check logs below"
    else
      check_warn "Last execution status: $EXEC_TYPE / $EXEC_STATUS"
    fi
  fi
fi

echo ""
echo -e "${BOLD}2. Cloud Scheduler${RESET}"
SCHED_STATE=$(gcloud scheduler jobs describe "$SCHEDULER_NAME" \
  --location="$REGION" \
  --project="$PROJECT_ID" \
  --format="value(state, schedule, timeZone, lastAttemptTime)" \
  2>/dev/null || echo "NOT_FOUND")

if [[ "$SCHED_STATE" == "NOT_FOUND" ]]; then
  check_fail "Scheduler job '$SCHEDULER_NAME' not found"
else
  SCHED_STATUS=$(echo "$SCHED_STATE" | awk '{print $1}')
  SCHED_CRON=$(echo "$SCHED_STATE"   | awk '{print $2}')
  SCHED_TZ=$(echo "$SCHED_STATE"     | awk '{print $3}')
  SCHED_LAST=$(echo "$SCHED_STATE"   | awk '{print $4}')

  if [[ "$SCHED_STATUS" == "ENABLED" ]]; then
    check_pass "Scheduler is ENABLED — cron: $SCHED_CRON ($SCHED_TZ)"
  else
    check_fail "Scheduler state: $SCHED_STATUS (expected ENABLED)"
  fi

  if [[ -n "$SCHED_LAST" && "$SCHED_LAST" != "None" ]]; then
    check_pass "Last triggered: $SCHED_LAST"
  else
    check_warn "Never triggered yet"
  fi
fi

echo ""
echo -e "${BOLD}3. Secret Manager${RESET}"
SECRETS=("GOOGLE_SHEET_ID" "GOOGLE_DRIVE_RESUME_FILE_ID" "JSEARCH_API_KEY" "GEMINI_API_KEY" "TELEGRAM_BOT_TOKEN" "TELEGRAM_CHAT_ID")
for secret in "${SECRETS[@]}"; do
  if gcloud secrets versions access latest --secret="$secret" --project="$PROJECT_ID" &>/dev/null; then
    check_pass "Secret readable: $secret"
  else
    check_fail "Secret not accessible: $secret"
  fi
done

echo ""
echo -e "${BOLD}4. Artifact Registry${RESET}"
IMAGE_INFO=$(gcloud artifacts docker images list \
  "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${JOB_NAME}" \
  --include-tags \
  --format="value(version, tags, createTime)" \
  --project="$PROJECT_ID" 2>/dev/null | head -3 || true)

if [[ -z "$IMAGE_INFO" ]]; then
  check_fail "No images found in Artifact Registry"
else
  DIGEST_COUNT=$(echo "$IMAGE_INFO" | grep -c "sha256" || true)
  check_pass "$DIGEST_COUNT image(s) in registry (should be 1 after cleanup)"
  if [[ "$DIGEST_COUNT" -gt 2 ]]; then
    check_warn "More than 2 images — run 'bash cicd.sh --update --skip-docker' to trigger cleanup"
  fi
fi

echo ""
echo -e "${BOLD}5. Recent Logs (last 20 lines from last execution)${RESET}"
RECENT_LOGS=$(gcloud logging read \
  "resource.type=cloud_run_job AND resource.labels.job_name=${JOB_NAME}" \
  --project="$PROJECT_ID" \
  --limit=20 \
  --format="value(timestamp, textPayload)" \
  --freshness=24h 2>/dev/null || true)

if [[ -z "$RECENT_LOGS" ]]; then
  check_warn "No logs found in last 24h — job may not have run yet"
else
  if echo "$RECENT_LOGS" | grep -q "Job Agent Starting"; then
    check_pass "Log signal: Job started successfully"
  fi
  if echo "$RECENT_LOGS" | grep -q "Resume extracted"; then
    check_pass "Log signal: Resume fetched from Drive"
  fi
  if echo "$RECENT_LOGS" | grep -q "Total jobs fetched"; then
    JOBS_LINE=$(echo "$RECENT_LOGS" | grep "Total jobs fetched" | tail -1)
    check_pass "Log signal: $JOBS_LINE"
  fi
  if echo "$RECENT_LOGS" | grep -q "Notified about"; then
    NOTIFY_LINE=$(echo "$RECENT_LOGS" | grep "Notified about" | tail -1)
    check_pass "Log signal: $NOTIFY_LINE"
  fi
  if echo "$RECENT_LOGS" | grep -qi "error\|exception\|traceback"; then
    check_fail "Errors detected in logs — review below:"
    echo "$RECENT_LOGS" | grep -i "error\|exception\|traceback" | head -5 | while read -r line; do
      echo -e "    ${RED}$line${RESET}"
    done
  fi

  echo ""
  echo -e "${BOLD}  Raw log tail:${RESET}"
  echo "$RECENT_LOGS" | tail -10 | while read -r line; do
    echo "    $line"
  done
fi

# ── Health check summary ──────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━ Health Check Summary ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
TOTAL=$((PASS + FAIL))
if [[ "$FAIL" -eq 0 ]]; then
  echo -e "  ${GREEN}${BOLD}All checks passed ($PASS/$TOTAL) ✔${RESET}"
else
  echo -e "  ${RED}${BOLD}$FAIL check(s) failed out of $TOTAL${RESET}"
  echo -e "  ${YELLOW}Review the ✘ items above and check logs:${RESET}"
  echo -e "  gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=${JOB_NAME}' \\"
  echo -e "    --project=$PROJECT_ID --limit=50 --format='table(timestamp,textPayload)' --freshness=1h"
fi

# ── Final summary (deployment mode only) ─────────────────────────────────────
if [[ "$HEALTH_CHECK_ONLY" != "true" ]]; then
  echo ""
  echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════╗"
  echo -e "║              Deployment Complete! ✔                 ║"
  echo -e "╚══════════════════════════════════════════════════════╝${RESET}"
  echo ""
  echo -e "  ${BOLD}Cloud Run Job :${RESET} $JOB_NAME ($REGION)"
  echo -e "  ${BOLD}Schedule      :${RESET} ${SCHEDULE} (${TIMEZONE})"
  echo -e "  ${BOLD}Image         :${RESET} $IMAGE"
  echo ""
  echo -e "  ${BOLD}Quick commands:${RESET}"
  echo -e "    bash cicd.sh --update              # Rebuild + redeploy after code change"
  echo -e "    bash cicd.sh --health-check        # Run checks anytime without deploying"
  echo -e "    gcloud run jobs execute $JOB_NAME --region=$REGION --wait"
  echo ""
fi