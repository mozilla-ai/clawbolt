# Home Depot sidecar: FastAPI + patchright Chromium.
#
# Two things here are load-bearing because Home Depot's bot manager reads them:
#
#   1. patchright, not playwright, for the patched CDP Runtime.enable leak.
#   2. The browser profile lives on a mounted volume so it survives redeploys.
#      A brand-new profile every boot looks like a first-time visitor every time.
#
# Running Chromium as a NON-ROOT user is hygiene, not a bot-detection
# requirement. patchright passes --no-sandbox by default, so the sandbox is off
# regardless of the user, and every working measurement was taken that way. Do
# not add chromium_sandbox=True expecting an improvement: it would add a
# user-namespace requirement the host may not grant, for no observed benefit.
#
# The container starts as root purely to fix volume ownership (platform volumes
# mount root-owned, so a plain USER directive leaves the app unable to write its
# profile), then drops to the unprivileged user with setpriv before exec'ing.
# The entrypoint has to set HOME across that handover; see the comment there.
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

# xvfb supplies the display a non-headless Chromium needs; the entrypoint starts
# Xvfb directly rather than via xvfb-run, so xauth is no longer required and is
# kept only so the wrapper still works if anyone reaches for it. util-linux
# supplies setpriv for the privilege drop. patchright pulls Chromium's own
# shared libraries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth util-linux \
    && patchright install-deps chromium \
    && patchright install chromium \
    && rm -rf /var/lib/apt/lists/*

# lowes.py is imported by sidecar.py at module level, so leaving it out makes the
# image die on `import lowes` before uvicorn binds a port: no /health to ask.
COPY sidecar.py lowes.py search_model.graphql ./

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
