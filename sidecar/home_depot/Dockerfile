# Retail sidecar: FastAPI + Camoufox (a hardened Firefox).
#
# Camoufox, not patchright/Chromium, is load-bearing: Home Depot's advanced
# Akamai tier fingerprints the CDP automation protocol every Chromium driver
# speaks over, and answers a 206 no matter how clean the fingerprint otherwise
# is (issue #1498). Camoufox is a patched Firefox with a consistent
# non-automated fingerprint and no CDP surface, which clears both retailers.
#
# No persistent profile and no volume. The browser is fetched fresh each launch
# and the session is validated by a humanized warm, so there is nothing to
# mount. The container still starts as root to drop to an unprivileged user with
# setpriv (running the browser as non-root is hygiene), and the entrypoint sets
# HOME across that handover; see the comment there.
#
# Build and run locally:
#   docker build -t hd-sidecar sidecar/home_depot
#   docker run --rm -p 8899:8899 -e HD_SIDECAR_TOKEN=secret hd-sidecar

# Pinned to bookworm: Playwright (Camoufox's driver dependency) only knows how
# to provision Firefox's system libraries on the Debian releases it supports,
# and install-deps fails outright on newer ones.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# xvfb supplies the display Camoufox needs when run headful (HD_HEADLESS unset);
# the entrypoint starts Xvfb directly rather than via xvfb-run, so xauth is kept
# only so the wrapper still works if anyone reaches for it. util-linux supplies
# setpriv/runuser for the privilege drop. `playwright install-deps firefox`
# pulls Firefox's shared libraries; playwright ships as a Camoufox dependency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth util-linux \
    && playwright install-deps firefox \
    && rm -rf /var/lib/apt/lists/*

# Globbed rather than listed. An earlier explicit list omitted lowes.py, which
# sidecar.py imports at module level, so the image died on `import lowes` before
# uvicorn bound a port, leaving no /health to ask what went wrong. A glob cannot
# forget the next module.
COPY *.py search_model.graphql ./

# Fetch the Camoufox browser AS the runtime user, so it lands in that user's
# cache (~/.cache/camoufox) where the app finds it after the privilege drop.
# The entrypoint sets XDG_CACHE_HOME=$HOME/.cache, so the paths line up. Fetching
# as root would put it under /root, unreadable by sidecar.
RUN useradd --create-home --shell /bin/bash sidecar \
    && chown -R sidecar:sidecar /app \
    && runuser -u sidecar -- python -m camoufox fetch

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# No VOLUME instruction and nothing to mount: the sidecar keeps no state between
# runs. A fresh browser plus the humanized warm looks like a first-time visitor
# every boot, which is fine, so there is no profile to persist.
EXPOSE 8899

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
