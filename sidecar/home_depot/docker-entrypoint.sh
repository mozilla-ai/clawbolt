#!/bin/sh
# Take ownership of the mounted profile volume, start a display, then run the
# app unprivileged.
#
# Platform volumes (Railway, Fly, plain `docker run -v`) mount root-owned and
# empty, so a bare USER directive in the Dockerfile leaves the app unable to
# write its browser profile. Start as root, hand the volume to the runtime user,
# and drop privileges before exec'ing. Running the browser as a normal user is
# ordinary hygiene here rather than a requirement: patchright passes
# --no-sandbox by default, so Chromium's sandbox is off either way.
#
# Every step announces itself. A container that dies silently between "Starting
# Container" and the first uvicorn line is otherwise undiagnosable from a
# platform log viewer, which is a situation this script has already caused once.
#
# Xvfb is started explicitly rather than through xvfb-run. The wrapper adds a
# hard dependency on xauth, picks a display number by scanning for locks, and
# sits between the app and the log stream. Doing it directly is one fewer layer
# to be wrong about.
set -eu

# To stderr, deliberately. stdout is block-buffered when it is a pipe, which is
# what a container log collector gives you, so progress messages sit in a 4KB
# buffer and are lost entirely if the process is killed before it fills. stderr
# is unbuffered, which is why the one error this script did emit historically
# (xvfb-run's) showed up while nothing else did.
log() { echo "[entrypoint] $*" >&2; }

PROFILE_DIR="${HD_PROFILE_DIR:-/data/profile}"
RUN_AS="${HD_RUN_AS_USER:-sidecar}"
PORT="${PORT:-8899}"
DISPLAY_NUM="${HD_DISPLAY:-99}"

log "uid=$(id -u) run_as=$RUN_AS port=$PORT profile=$PROFILE_DIR"

mkdir -p "$PROFILE_DIR"
log "profile dir present"

if [ "$(id -u)" -eq 0 ]; then
    chown -R "$RUN_AS:$RUN_AS" "$(dirname "$PROFILE_DIR")"
    log "volume ownership handed to $RUN_AS"
else
    log "not root, skipping chown"
fi

log "python: $(python --version 2>&1)"

Xvfb ":$DISPLAY_NUM" -screen 0 1440x900x24 -nolisten tcp &
XVFB_PID=$!
sleep 2
if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    log "FATAL: Xvfb died immediately on :$DISPLAY_NUM"
    exit 1
fi
export DISPLAY=":$DISPLAY_NUM"
log "Xvfb up on $DISPLAY (pid $XVFB_PID)"

log "exec uvicorn on 0.0.0.0:$PORT"
if [ "$(id -u)" -ne 0 ]; then
    # Already unprivileged (some platforms pin the UID). Nothing to drop.
    exec python -m uvicorn sidecar:app --host 0.0.0.0 --port "$PORT"
fi

# setpriv switches uid/gid but leaves the environment alone, so HOME would stay
# /root: a directory the unprivileged user cannot write. Chromium's crashpad
# handler then fails with "--database is required" and the browser dies with
# SIGTRAP before it ever opens a page. `su` sets HOME, which is why this only
# broke under setpriv. XDG_* follow HOME for the same reason.
RUN_HOME="$(getent passwd "$RUN_AS" | cut -d: -f6)"
RUN_HOME="${RUN_HOME:-/home/$RUN_AS}"
mkdir -p "$RUN_HOME"
chown "$RUN_AS:$RUN_AS" "$RUN_HOME"
log "handing over with HOME=$RUN_HOME"

exec setpriv --reuid "$RUN_AS" --regid "$RUN_AS" --init-groups \
    env HOME="$RUN_HOME" \
        XDG_CACHE_HOME="$RUN_HOME/.cache" \
        XDG_CONFIG_HOME="$RUN_HOME/.config" \
        python -m uvicorn sidecar:app --host 0.0.0.0 --port "$PORT"
