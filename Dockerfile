# ── Base image ────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Install dependencies (cached layer — only rebuilds if requirements change) ─
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy application source ───────────────────────────────────────────────────
# .dockerignore excludes: venv, __pycache__, .env, serviceAccountKey.json, etc.
COPY . .

# ── Port ──────────────────────────────────────────────────────────────────────
# Cloud Run injects $PORT at runtime (usually 8080).
# EXPOSE is documentation only; the actual binding is done by gunicorn below.
EXPOSE 8080

# ── Run ───────────────────────────────────────────────────────────────────────
# Shell form (not JSON array) so $PORT is expanded by the shell.
# --threads 8 lets a single worker handle concurrent requests efficiently on
# Cloud Run's single-vCPU instances without the overhead of multiple processes.
CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120 run:app
