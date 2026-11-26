#!/usr/bin/env bash
# Isolated ingestion-to-database HTTP smoke. No provider connection or persistent host data.
set -euo pipefail
repo_root=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo_root"
db_image=${DB_IMAGE:-algot-ci-database:local}
ingestion_image=${INGESTION_IMAGE:-algot-ci-ingestion:local}
if [[ -z "${DB_IMAGE:-}" ]]; then docker build -t "$db_image" Data/Database >/dev/null; fi
if [[ -z "${INGESTION_IMAGE:-}" ]]; then docker build -t "$ingestion_image" Data/Data_Ingestion >/dev/null; fi
network="algot-ci-net-$$"
db_name="algot-ci-db-$$"
cleanup() {
    docker stop "$db_name" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create "$network" >/dev/null
docker run -d --rm --name "$db_name" --network "$network" --network-alias market-data-db \
    --tmpfs /data:rw,uid=10001,gid=10001 --read-only --tmpfs /tmp:rw,nosuid,nodev \
    --cap-drop ALL --security-opt no-new-privileges \
    -e MARKET_DATA_DB_API_KEYS=isolated-ci-key "$db_image" >/dev/null
ready=false
for _ in $(seq 1 30); do
    status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' "$db_name")
    if [[ "$status" == healthy ]]; then ready=true; break; fi
    if [[ "$status" == unhealthy ]]; then break; fi
    sleep 2
done
[[ "$ready" == true ]] || { echo 'isolated database failed readiness' >&2; exit 1; }
docker run --rm -i --network "$network" --entrypoint python "$ingestion_image" - <<'PY'
import httpx
url = 'http://market-data-db:8000'
headers = {'X-API-Key': 'isolated-ci-key'}
record = {
    'symbol': 'AAPL', 'asset_class': 'equity', 'timestamp': '2024-01-02T14:30:00+00:00',
    'price': 190.5, 'size': 100, 'exchange': 'V', 'trade_id': '12345',
    'conditions': '@', 'tape': 'C',
}
with httpx.Client(base_url=url, headers=headers, timeout=10) as client:
    for _ in range(2):
        response = client.post('/v1/datasets/trades/write', json={
            'provider': 'alpaca', 'source': 'live', 'records': [record],
        })
        response.raise_for_status()
    response = client.post('/v1/datasets/trades/read', json={
        'symbols': ['AAPL'], 'dedup': 'exact',
    })
    response.raise_for_status()
    rows = response.json()['rows']
    assert len(rows) == 1, rows
    assert rows[0]['price'] == 190.5, rows
print('isolated container network and exact deduplication passed')
PY
