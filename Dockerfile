# ── Stage 1: dependency builder ──────────────────────────────────────────────
# Separate stage so pip build tools (gcc etc.) never reach the final image
FROM python:3.11-slim AS builder

WORKDIR /build

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# PYTHONUNBUFFERED=1 → logs appear immediately in Cloud Run (not buffered)
# PYTHONDONTWRITEBYTECODE=1 → no .pyc clutter inside container
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/deps/lib/python3.11/site-packages

WORKDIR /app

# Copy only the installed packages from builder — no build tools in final image
COPY --from=builder /install /app/deps

# Non-root user — never run containers as root in production
RUN useradd --no-create-home --shell /bin/false appuser

# Copy source code
COPY . .

# Drop to non-root before running
USER appuser

CMD ["python", "main.py"]