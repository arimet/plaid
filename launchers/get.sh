#!/bin/sh
# Plaid launcher for Linux PCs, mini PCs, Synology NAS and Macs (no Gatekeeper prompt, unlike install.command).
#   curl -fsSL https://raw.githubusercontent.com/arimet/plaid/main/launchers/get.sh | sh
#   ... | sh -s -- --cli         same questions, in the terminal
#   ... | sh -s -- --uninstall   remove the stack from the terminal
set -eu

IMAGE="${PLAID_IMAGE:-ghcr.io/arimet/plaid:latest}"
MODE="${1:-}"
NAME=plaid

if [ -f /etc/synoinfo.conf ]; then PLATFORM=synology
elif [ "$(uname -s)" = Darwin ]; then PLATFORM=mac
else PLATFORM=linux; fi

if ! command -v docker >/dev/null 2>&1; then
  if [ "$PLATFORM" = mac ]; then
    echo "Docker Desktop is missing: https://github.com/arimet/plaid#install-docker-desktop"
    exit 1
  fi
  if [ "$PLATFORM" = synology ]; then
    echo "Docker is missing. In DSM, open Package Center, install Container Manager, then run this command again."
    exit 1
  fi
  echo "Installing Docker (this takes a few minutes)..."
  curl -fsSL https://get.docker.com | sh
fi

if [ "$PLATFORM" = mac ]; then
  # Docker Desktop may be installed but not started: start it and wait, as install.command does.
  export PATH="$PATH:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin"
  until docker info >/dev/null 2>&1; do
    [ -d /Applications/Docker.app ] && open -g -a Docker
    echo "Waiting for Docker Desktop..."
    sleep 5
  done
  # Start Docker Desktop at login, or the apps stop when the Mac restarts. Best effort.
  plutil -replace AutoStart -bool YES "$HOME/Library/Group Containers/group.com.docker/settings-store.json" \
    >/dev/null 2>&1 || true
fi

DOCKER="docker"
if ! docker info >/dev/null 2>&1; then DOCKER="sudo docker"; fi

if [ "$PLATFORM" = synology ] && [ ! -e /dev/net/tun ]; then
  echo "Note: /dev/net/tun is missing; the VPN will not work on this NAS."
fi

ADDR=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
[ -n "$ADDR" ] || ADDR=$(ip route get 1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' || true)
[ -n "$ADDR" ] || ADDR=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)  # macOS
[ -n "$ADDR" ] || ADDR=localhost
TZ_NAME=$(cat /etc/timezone 2>/dev/null || readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##' || echo UTC)
[ -n "$TZ_NAME" ] || TZ_NAME=UTC

run() {
  # shellcheck disable=SC2086
  $DOCKER run -v /var/run/docker.sock:/var/run/docker.sock -v plaid-state:/state \
    -e HOST_HOME="$HOME" -e HOST_PLATFORM="$PLATFORM" -e HOST_ADDR="$ADDR" -e TZ="$TZ_NAME" \
    -e PUID="$(id -u)" -e PGID="$(id -g)" -e LANG="${LANG:-en}" --label io.plaid=1 "$@"
}

start_panel() {
  # An older Plaid container is replaced by the image just pulled: its settings live in the plaid-state volume.
  if $DOCKER container inspect "$NAME" >/dev/null 2>&1 &&
     [ "$($DOCKER container inspect -f '{{.Image}}' "$NAME")" != "$($DOCKER image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null)" ]; then
    $DOCKER rm -f "$NAME" >/dev/null
  fi
  if $DOCKER container inspect "$NAME" >/dev/null 2>&1; then
    $DOCKER start "$NAME" >/dev/null
  else
    run -d --name "$NAME" --restart unless-stopped -p 7575:7575 "$IMAGE" >/dev/null || {
      $DOCKER rm -f "$NAME" >/dev/null 2>&1
      echo "Plaid could not start: port 7575 is probably used by another program."
      exit 1
    }
  fi
  echo
  echo "Plaid is ready. Open this address in a browser on this network:"
  echo "    http://$ADDR:7575"
  if [ "$PLATFORM" = mac ]; then  # the Mac has a screen: open the assistant there
    until curl -fs http://localhost:7575/api/ping >/dev/null 2>&1; do sleep 1; done
    open http://localhost:7575
  fi
}

# After a terminal run that left neither a stack nor an assistant (cancelled, rolled back or removed),
# take the state volume and the image away too: the machine is as it was. The terminal mode itself
# holds the volume while it runs, so this can only happen here, once it has exited.
leave_no_trace() {
  if [ -z "$($DOCKER ps -aq --filter label=com.docker.compose.project=plaid)" ] &&
     ! $DOCKER container inspect "$NAME" >/dev/null 2>&1; then
    $DOCKER volume rm plaid-state >/dev/null 2>&1 || true
    $DOCKER rmi "$IMAGE" >/dev/null 2>&1 || true  # still used by the cleanup container: the reaper takes it
  fi
}

export DOCKER_CLI_HINTS=false  # no "What's next" advert from Docker Desktop after a cancelled run
$DOCKER pull -q "$IMAGE" >/dev/null 2>&1 || true
# Closing the terminal kills the docker client but not the container, which then waits forever for an
# answer and holds the state volume. The terminal mode has a fixed name, so a leftover one goes first.
CLI=plaid-cli
$DOCKER rm -f "$CLI" >/dev/null 2>&1 || true
case "$MODE" in
  --cli)
    # Installs, or on an installed machine opens the terminal panel (apps, trackers, removal).
    status=0
    run --rm -it --name "$CLI" "$IMAGE" --cli </dev/tty || status=$?
    if [ -n "$($DOCKER ps -aq --filter label=com.docker.compose.project=plaid)" ]; then
      start_panel >/dev/null  # keep the web assistant up for other computers on the network
    else
      leave_no_trace
    fi
    exit "$status" ;;
  --uninstall)
    run --rm -it --name "$CLI" "$IMAGE" --uninstall </dev/tty || true
    leave_no_trace ;;
  *)
    start_panel ;;
esac
