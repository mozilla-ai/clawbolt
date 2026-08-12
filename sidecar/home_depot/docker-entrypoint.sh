#!/bin/sh
# Start a display, then run the app unprivileged.
#
# There is no profile volume any more: Camoufox runs a fresh browser each launch
# and validates the session with a humanized warm, so there is nothing to mount
# or chown. The container still starts as root only to drop to the runtime user
# with a writable HOME before exec'ing (see the setpriv handover below); running
# the browser as a normal user is hygiene, not a bot-detection requirement.
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

RUN_AS="${HD_RUN_AS_USER:-sidecar}"
PORT="${PORT:-8899}"
DISPLAY_NUM="${HD_DISPLAY:-99}"

# No persistent profile to own: Camoufox runs a fresh browser each launch and
# validates the session with a humanized warm, so there is no volume to mount or
# chown. The runtime user still needs a writable HOME; that is set at the
# privilege drop below.
log "uid=$(id -u) run_as=$RUN_AS port=$PORT"

log "python: $(python --version 2>&1)"

# A restart is not a redeploy: it reuses the container filesystem, so the lock
# and socket from the previous run are still on disk and Xvfb refuses to start
# on a display it believes is already live. Nothing else in this container owns
# the display, so a leftover is always stale. Without this, a platform-initiated
# restart exits FATAL, exhausts restartPolicyMaxRetries in about ten seconds,
# and leaves the deployment CRASHED until a human redeploys. `-f` matters: the
# files are absent on a fresh container and `set -e` would abort on the failure.
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

Xvfb ":$DISPLAY_NUM" -screen 0 1440x900x24 -nolisten tcp &
XVFB_PID=$!
sleep 2
if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    # Xvfb writes its own reason to stderr, which reaches the platform log
    # ahead of this line. Name the lock anyway: it is the cause that took a
    # production outage to find, and the wording is what an operator greps for.
    log "FATAL: Xvfb died immediately on :$DISPLAY_NUM (stale /tmp/.X${DISPLAY_NUM}-lock?)"
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
# /root: a directory the unprivileged user cannot write, which breaks Firefox
# startup. `su` sets HOME, which is why this kind of bug only shows up under
# setpriv. XDG_CACHE_HOME follows HOME because that is where Camoufox looks for
# the browser fetched at build time (~/.cache/camoufox); the image fetches it as
# this same user, so the paths line up.
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
