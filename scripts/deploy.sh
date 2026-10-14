#!/bin/sh
# Apply the current checkout on the NAS: fix folder permissions, rebuild, up -d.
# Usage: scripts/deploy.sh <previous commit>
#   The caller fast-forwards the checkout first. If the build or start fails, the checkout is
#   reset to <previous commit>; a failed build leaves the running container untouched.
# NAS-only settings (PUID, REC_DIR, OIDC) live in .env next to the compose file.
set -eu

cd "$(dirname "$0")/.."

D="sudo -n /usr/local/bin/docker"
prev="${1:?usage: deploy.sh <previous commit>}"

rollback() {
  trap - EXIT
  echo "deploy: FAILED, resetting checkout to $(git log -1 --format='%h' "$prev")" >&2
  git reset -q --hard "$prev"
  exit 1
}
trap rollback EXIT

echo "deploy: $(git log -1 --format='%h %s') (was $(git log -1 --format='%h' "$prev"))"

REC_DIR=$(grep -E '^REC_DIR=' .env | cut -d= -f2-)
mkdir -p "$REC_DIR" data/cookies
# A shared-folder ACL inherited by these folders makes them unwritable inside the container.
for p in data "$REC_DIR"; do /usr/syno/bin/synoacltool -del "$p" >/dev/null 2>&1 || true; done
chmod 755 data "$REC_DIR"
chmod 700 data/cookies

$D compose config -q
$D compose up -d --build 2>&1 | tail -3

trap - EXIT
echo "deploy: ok"
