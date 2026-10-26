# Control-plane image: API + dashboard only. It does NOT run agent CLIs;
# those run on the host via the agentbridge-engine systemd service.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/agentbridge
COPY pyproject.toml README.md ./
COPY agentbridge ./agentbridge
RUN pip install --no-cache-dir . && useradd --system --uid 10001 --home /var/lib/agentbridge agentbridge
COPY tests ./tests
USER agentbridge
ENV AGENTBRIDGE_DATA_DIR=/var/lib/agentbridge AGENTBRIDGE_CONFIG=/etc/agentbridge/agentbridge.yaml
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:8787/api/health || exit 1
CMD ["agentbridge", "serve", "--host", "0.0.0.0", "--port", "8787"]
