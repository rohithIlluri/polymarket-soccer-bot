FROM python:3.12-slim

# Git is required for the autoresearch commit/revert loop
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files
COPY pyproject.toml .
COPY *.py ./
COPY program.md .
COPY .gitignore .

# Install dependencies
RUN pip install --no-cache-dir .

# Initialize git repo (needed for autoresearch loop)
RUN git init && \
    git config user.email "bot@polymarket.local" && \
    git config user.name "PolyBot"

# Health check via health file
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import json,sys,time; h=json.load(open('/tmp/bot_health.json')); age=time.time()-__import__('datetime').datetime.fromisoformat(h['last_run']).timestamp(); sys.exit(0 if age<7200 else 1)" || exit 1

CMD ["python", "run.py"]
