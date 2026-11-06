#!/bin/bash
# Plaid launcher for macOS: double-click it. It needs Docker Desktop and opens the assistant in your browser.
set -u
IMAGE="${PLAID_IMAGE:-ghcr.io/arimet/plaid:latest}"
NAME=arr-installer
HELP="https://github.com/arimet/plaid#install-docker-desktop"

if [ ! -d /Applications/Docker.app ] && ! command -v docker >/dev/null 2>&1; then
  echo "Docker Desktop is missing. Install it, then this window carries on by itself."
  open "$HELP"
fi
export PATH="$PATH:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin"
until docker info >/dev/null 2>&1; do
  [ -d /Applications/Docker.app ] && open -g -a Docker
  sleep 5
done

# Start Docker Desktop at login, or the apps stop when the Mac restarts. Best effort: macOS may refuse access.
STORE="$HOME/Library/Group Containers/group.com.docker/settings-store.json"
plutil -replace AutoStart -bool YES "$STORE" >/dev/null 2>&1 || true

TZ_NAME=$(readlink /etc/localtime | sed 's#.*/zoneinfo/##')
if docker container inspect "$NAME" >/dev/null 2>&1; then
  docker start "$NAME" >/dev/null
else
  docker run -d --name "$NAME" --restart unless-stopped -p 7575:7575 \
    -v /var/run/docker.sock:/var/run/docker.sock -v arr-installer-state:/state \
    -e HOST_HOME="$HOME" -e HOST_PLATFORM=mac -e HOST_ADDR=localhost -e TZ="$TZ_NAME" \
    -e PUID="$(id -u)" -e PGID="$(id -g)" --label io.arr-installer=1 "$IMAGE" >/dev/null || {
      docker rm -f "$NAME" >/dev/null 2>&1
      echo "Plaid could not start: port 7575 is probably used by another program."
      exit 1
    }
fi
until curl -fs http://localhost:7575/api/ping >/dev/null 2>&1; do sleep 1; done
open http://localhost:7575
echo "Plaid is open in your browser: http://localhost:7575. You can close this window."
