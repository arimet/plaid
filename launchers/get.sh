#!/bin/sh
# Plaid launcher for Linux PCs, mini PCs and Synology NAS.
#   curl -fsSL https://raw.githubusercontent.com/arimet/plaid/main/launchers/get.sh | sh
#   ... | sh -s -- --cli         same questions, in the terminal
#   ... | sh -s -- --uninstall   remove the stack from the terminal
set -eu

IMAGE="${PLAID_IMAGE:-ghcr.io/arimet/plaid:latest}"
MODE="${1:-}"
NAME=arr-installer

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

DOCKER="docker"
if ! docker info >/dev/null 2>&1; then DOCKER="sudo docker"; fi

if [ "$PLATFORM" = synology ] && [ ! -e /dev/net/tun ]; then
  echo "Note: /dev/net/tun is missing; the VPN will not work on this NAS."
fi

ADDR=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
[ -n "$ADDR" ] || ADDR=$(ip route get 1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' || true)
[ -n "$ADDR" ] || ADDR=localhost
TZ_NAME=$(cat /etc/timezone 2>/dev/null || readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##' || echo UTC)
[ -n "$TZ_NAME" ] || TZ_NAME=UTC

run() {
  # shellcheck disable=SC2086
  $DOCKER run -v /var/run/docker.sock:/var/run/docker.sock -v arr-installer-state:/state \
    -e HOST_HOME="$HOME" -e HOST_PLATFORM="$PLATFORM" -e HOST_ADDR="$ADDR" -e TZ="$TZ_NAME" \
    -e PUID="$(id -u)" -e PGID="$(id -g)" -e LANG="${LANG:-en}" --label io.arr-installer=1 "$@"
}

start_panel() {
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
}

$DOCKER pull -q "$IMAGE" >/dev/null 2>&1 || true
case "$MODE" in
  --cli)
    run --rm -it "$IMAGE" --cli </dev/tty && start_panel ;;
  --uninstall)
    run --rm -it "$IMAGE" --uninstall </dev/tty ;;
  *)
    start_panel ;;
esac
