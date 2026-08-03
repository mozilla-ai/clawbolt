# Home Depot sidecar: FastAPI + patchright Chromium.
#
# UNTESTED: built by reading the platform docs, not by running it. The steps it
# performs are each verified on a live host (patchright install, non-root
# Chromium under Xvfb, the app itself), but this exact image has never been
# built. Expect to iterate on the first deploy.
#
# Three things here are load-bearing rather than stylistic, because Home Depot's
# bot manager reads all of them:
#
#   1. Chromium runs as a NON-ROOT user so its own sandbox stays enabled.
#      Running it as root would force --no-sandbox, a strong bot signal. This
#      needs unprivileged user namespaces on the host; if the platform forbids
#      them Chromium refuses to start, which is a deliberate hard failure rather
#      than a silent downgrade.
#   2. The browser profile lives on a mounted volume so it survives redeploys.
#      A brand-new profile every boot looks like a first-time visitor every time.
#   3. patchright, not playwright, for the patched CDP Runtime.enable leak.
#
# The container starts as root purely to fix volume ownership (platform volumes
# mount root-owned, so a plain USER directive leaves the app unable to write its
# profile), then drops to the unprivileged user with setpriv before exec'ing.
#
# Build and run locally:
#   docker build -t hd-sidecar sidecar/home_depot
#   docker run --rm -p 8899:8899 -e HD_SIDECAR_TOKEN=secret hd-sidecar

# Pinned to bookworm: patchright's install-deps only knows how to provision
# Chromium's system libraries on the Debian releases Playwright supports, and
# it fails outright on newer ones.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers \
    HD_PROFILE_DIR=/data/profile

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# xvfb supplies the display a non-headless Chromium needs; util-linux supplies
# setpriv for the privilege drop; patchright pulls Chromium's own libraries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb util-linux \
    && patchright install-deps chromium \
    && patchright install chromium \
    && rm -rf /var/lib/apt/lists/*

COPY sidecar.py search_model.graphql ./

RUN useradd --create-home --shell /bin/bash sidecar \
    && chown -R sidecar:sidecar /app \
    && chmod -R a+rX /opt/browsers

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# No VOLUME instruction: Railway's builder rejects it outright ("docker VOLUME
# is not supported, use Railway Volumes"), and persistence is the deployer's
# call regardless. Mount something at /data or the profile is rebuilt on every
# restart, which makes each boot look like a first-time visitor:
#   Railway: add a volume with mount path /data
#   docker:  -v hd-profile:/data
EXPOSE 8899

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
