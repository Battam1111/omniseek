# OmniSeek: self-hosted deep-retrieval MCP. CORE (Apache-clean) image: no AGPL PDF lib, no
# torch, no circumvention signer. Opt into extras at build time:  --build-arg EXTRAS="[pdf,asr]".
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMNISEEK_HTTP_HOST=0.0.0.0 \
    OMNISEEK_HTTP_PORT=8765

# System deps for the chromium scrape/render path. ffmpeg is only needed by the optional [asr]
# extra (imageio-ffmpeg vendors its own binary), so the core image omits it.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY deploy/profile.example.json ./profile.example.json
COPY src ./src
COPY scripts ./scripts
COPY tests ./tests

# Core install is Apache-clean. EXTRAS opt-in (you accept their licenses; see NOTICE):
#   docker build --build-arg EXTRAS="[pdf,asr,walled]" .
ARG EXTRAS=""
RUN pip install -e ".${EXTRAS}"

# Chromium for the scrape/render path (playwright does NOT pip-install the browser binary).
RUN python -m playwright install --with-deps chromium || python -m playwright install chromium

COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8765
# State (credentials + bearer token + profile + cache + recall index + curator state + downloaded
# ASR model weights) lives under /root/.omniseek; the omniseek_read document inbox under
# /root/omniseek-inbox. Mount both to persist.
VOLUME ["/root/.omniseek", "/root/omniseek-inbox"]

# Liveness: /healthz is open (no token).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${OMNISEEK_HTTP_PORT}/healthz" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "omniseek.serve_http"]
