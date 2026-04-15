FROM python:3.11-slim

LABEL maintainer="balakrishnav171"
LABEL description="BaluAgent — Agentic Job Search Automation"

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create non-root user
RUN useradd -m -u 1000 baluagent && chown -R baluagent:baluagent /app
USER baluagent

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8765/health || exit 1

EXPOSE 8765

CMD ["python", "main.py", "schedule-daemon", "--interval-hours", "24"]
