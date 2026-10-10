#!/usr/bin/env bash
# Pobiera zmiany z GitHuba i przebudowuje kontenery, ale tylko gdy są nowe commity.
# Uruchamiaj z crona na hoście Dockera, np.:
#   */5 * * * * /home/USER/Rachunki/domowe-rachunki/deploy.sh >> /home/USER/deploy.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"

git fetch --quiet origin
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse '@{u}')

if [ "$LOCAL" = "$REMOTE" ]; then
  echo "$(date '+%F %T') bez zmian"
  exit 0
fi

echo "$(date '+%F %T') nowe zmiany: ${LOCAL:0:7} -> ${REMOTE:0:7}"
git pull --ff-only --quiet
export GIT_SHA=$(git rev-parse --short HEAD)
docker compose up -d --build
docker image prune -f >/dev/null
echo "$(date '+%F %T') wdrożono ${REMOTE:0:7}"
