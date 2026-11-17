FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PROJECTS_ROOT=/workspace/projects \
    LLM_BACKEND=bob_shell \
    NODE_OPTIONS=--use-openssl-ca \
    PORT=5000 \
    MCP_PORT=8001

WORKDIR /app

COPY requirements.txt .
# git: used by the planned Jenkins/branch flow; curl + nodejs/npm: Bob Shell CLI (same as docker/Dockerfile).
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

# Bob Shell CLI (official installer). The licence is accepted once at build time as root, and the
# resulting ~/.bob state is copied to the app user so the first run does not prompt.
# Auth is NOT baked in: pass BOBSHELL_API_KEY at runtime (--env-file .env).
RUN curl -fsSL https://bob.ibm.com/download/bobshell.sh | bash \
    && (HOME=/root bob --accept-license </dev/null 2>&1; true)

COPY . .

# Run as a non-root user. /data holds runs, cases, the project registry and the knowledge base.
# /workspace/projects is where the repositories under analysis are mounted (read-only is enough).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data /workspace/projects \
    && if [ -d /root/.bob ]; then cp -r /root/.bob /home/appuser/.bob; fi \
    && chown -R appuser:appuser /data /app /home/appuser
USER appuser

VOLUME ["/data"]
EXPOSE 5000 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','5000')+'/healthz',timeout=3)"

# Web UI on :5000 and MCP server (streamable-http) on :8001/mcp
CMD ["python", "run.py"]
