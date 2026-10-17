#!/usr/bin/env bash
# Nightly Postgres dump (the only state that can't be rebuilt: OpenSearch and Neo4j are
# re-derived from Postgres by `reindex`). Keeps the last 7 days.
set -euo pipefail

cd "$(dirname "$0")"
BACKUP_DIR="${BACKUP_DIR:-$(cd ../.. && pwd)/backups}"
mkdir -p "$BACKUP_DIR"
target="$BACKUP_DIR/support-$(date -u +%Y%m%dT%H%M%SZ).dump"

docker compose -f docker-compose.prod.yml exec -T postgres pg_dump -U support -Fc support > "$target.partial"
mv "$target.partial" "$target"
find "$BACKUP_DIR" -name 'support-*.dump' -mtime +7 -delete
echo "Backup written: $target ($(du -h "$target" | cut -f1))"
