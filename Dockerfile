# OmniSeek: self-hosted deep-retrieval MCP. CORE (Apache-clean) image: no AGPL PDF lib, no
# torch, no circumvention signer. Opt into extras at build time:  --build-arg EXTRAS="[pdf,asr]".
# Python 3.12, the version deploy/requirements.lock is compiled for. Pinned by digest, which wins over
# the tag: it is what the tag python:3.12.16-slim-bookworm pointed to on 2026-11-11 (Docker Hub's tag
# API). An unpinned slim tag once moved under the build (2026-11-11: the new base lacked libatomic and
# playwright's node would not start).
FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMNISEEK_HTTP_HOST=0.0.0.0 \
    OMNISEEK_HTTP_PORT=8765 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# System deps for the chromium scrape/render path. ffmpeg is only needed by the optional [asr]
# extra (imageio-ffmpeg vendors its own binary), so the core image omits it.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl libatomic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY deploy/profile.example.json ./profile.example.json
COPY deploy/requirements.lock ./deploy/requirements.lock
COPY src ./src
COPY scripts ./scripts

# Core install is Apache-clean. EXTRAS opt-in (you accept their licenses; see NOTICE):
#   docker build --build-arg EXTRAS="[pdf,asr,walled]" .
# Every version comes from deploy/requirements.lock, used as a pip constraints file: it fixes the
# version of whatever gets installed and installs nothing by itself, so the AGPL and torch packages
# the lock also lists stay out of the core image. The hashes are cut first: one --hash anywhere turns
# on pip's hash-checking mode for the whole install, and that mode refuses the editable install.
# pip itself is brought to the lock's version before it installs anything else.
ARG EXTRAS=""
RUN sed -n 's/^\([A-Za-z0-9._-]*==[^ ]*\).*/\1/p' deploy/requirements.lock > /tmp/constraints.txt \
    && pip install -c /tmp/constraints.txt pip \
    && pip install -c /tmp/constraints.txt -e ".${EXTRAS}" \
    && rm /tmp/constraints.txt

# Chromium for the scrape/render path (playwright does NOT pip-install the browser binary). The
# browser build is the one the locked playwright names; when its system libraries cannot be
# installed the build fails here.
RUN python -m playwright install --with-deps chromium

COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8765
# Everything OmniSeek keeps (credentials + bearer token + profile + cache + recall index + curator
# state + downloaded models, and the omniseek_read document inbox, inbox/) lives under
# /root/.omniseek, at the places omniseek.where names: `python -m omniseek.where state` prints one of
# them. Mount that folder to persist. (The browser binary sits outside it, in PLAYWRIGHT_BROWSERS_PATH.)
VOLUME ["/root/.omniseek"]

# Liveness: /healthz is open (no token).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${OMNISEEK_HTTP_PORT}/healthz" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "omniseek.serve_http"]
