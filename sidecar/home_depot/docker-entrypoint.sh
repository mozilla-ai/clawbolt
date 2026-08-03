#!/bin/sh
# Take ownership of the mounted profile volume, then run the app unprivileged.
#
# Platform volumes (Railway, Fly, plain `docker run -v`) mount root-owned and
# empty, so a bare USER directive in the Dockerfile leaves the app unable to
# write its browser profile. Start as root, hand the volume to the runtime user,
# and drop privileges before exec'ing: Chromium must not run as root or its
# sandbox is disabled, which is exactly the signal we are avoiding.
set -eu

PROFILE_DIR="${HD_PROFILE_DIR:-/data/profile}"
RUN_AS="${HD_RUN_AS_USER:-sidecar}"
PORT="${PORT:-8899}"

mkdir -p "$PROFILE_DIR"
chown -R "$RUN_AS:$RUN_AS" "$(dirname "$PROFILE_DIR")"

if [ "$(id -u)" -ne 0 ]; then
    # Already unprivileged (some platforms pin the UID). Nothing to drop.
    exec xvfb-run -a python -m uvicorn sidecar:app --host 0.0.0.0 --port "$PORT"
fi

exec setpriv --reuid "$RUN_AS" --regid "$RUN_AS" --init-groups \
    xvfb-run -a python -m uvicorn sidecar:app --host 0.0.0.0 --port "$PORT"
