#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
docker info >/dev/null
docker compose up --build --detach --wait --wait-timeout 180
printf 'Paper platform ready: http://%s/\n' "$(docker compose port dashboard 8765)"
