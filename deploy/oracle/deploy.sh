#!/usr/bin/env bash
# Build and (re)start the production stack from the currently checked-out commit.
# CD (.github/workflows/deploy.yml) checks out the target commit first, then runs this.
set -euo pipefail

cd "$(dirname "$0")"
compose() { docker compose -f docker-compose.prod.yml "$@"; }

[ -f .env ] || { echo "deploy/oracle/.env missing (see .env.example)" >&2; exit 1; }

echo "==> Deploying $(git rev-parse --short HEAD)"
compose build --pull
compose up -d --wait postgres
compose run --rm migrate
compose up -d --remove-orphans --wait

echo "==> Health check"
for _ in $(seq 1 20); do
  if compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/readyz')"; then
    echo "API ready"
    docker image prune -f >/dev/null
    exit 0
  fi
  sleep 3
done
echo "API did not become ready" >&2
compose ps
exit 1
