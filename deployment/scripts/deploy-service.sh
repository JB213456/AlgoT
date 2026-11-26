#!/usr/bin/env bash
# Controlled single-service replacement. Never removes volumes or database records.
set -euo pipefail
service=${1:?service required}
target=${2:?target required}
image=${3:?immutable image digest required}
mode=${4:-deploy}
case "$service" in database|ingestion) ;; *) echo 'unknown service' >&2; exit 2 ;; esac
case "$target" in staging|production) ;; *) echo 'unknown target' >&2; exit 2 ;; esac
[[ "$image" =~ ^ghcr\.io/[a-z0-9_.-]+/algot-$service@sha256:[a-f0-9]{64}$ ]] || { echo 'image must be a service-matched GHCR digest' >&2; exit 2; }
case "$mode" in deploy|dry-run) ;; *) exit 2 ;; esac
config_root=${ALGOT_DEPLOY_CONFIG_ROOT:-/etc/algot}
state_root=${ALGOT_DEPLOY_STATE_ROOT:-/var/lib/algot-deploy}
env_file="$config_root/$target/$service.env"
[[ -r "$env_file" ]] || { echo "missing service environment file: $env_file" >&2; exit 2; }
name="algot-$target-$service"
network="algot-$target-net"
if [[ "$service" == database ]]; then
    volume="algot-$target-market-data-lake"
    alias="market-data-db"
    memory=2g
    cpus=2
else
    volume="algot-$target-ingestion-checkpoints"
    alias="data-ingestion"
    memory=1g
    cpus=2
fi
if [[ "$mode" == dry-run ]]; then
    printf 'validated target=%s service=%s image=%s container=%s volume=%s network=%s\n' "$target" "$service" "$image" "$name" "$volume" "$network"
    exit 0
fi
mkdir -p "$state_root"
exec 9>"$state_root/$target-$service.lock"
flock -n 9 || { echo 'another deployment is active' >&2; exit 1; }
# The database must already be healthy before ingestion takes over its subscriptions.
if [[ "$service" == ingestion ]]; then
    db_status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' "algot-$target-database")
    [[ "$db_status" == healthy ]] || { echo 'database is not healthy' >&2; exit 1; }
fi
docker image pull "$image"
docker network inspect "$network" >/dev/null 2>&1 || docker network create "$network" >/dev/null
docker volume inspect "$volume" >/dev/null 2>&1 || docker volume create "$volume" >/dev/null
previous=''
if docker container inspect "$name" >/dev/null 2>&1; then
    previous="${name}-previous-$(date -u +%Y%m%dT%H%M%SZ)"
    docker stop --time 35 "$name" >/dev/null
    docker rename "$name" "$previous"
fi
restore_previous() {
    docker stop --time 10 "$name" >/dev/null 2>&1 || true
    docker rm "$name" >/dev/null 2>&1 || true
    if [[ -n "$previous" ]]; then
        docker rename "$previous" "$name"
        docker start "$name" >/dev/null
    fi
}
if ! docker run -d --name "$name" --hostname "$alias" --network "$network" --network-alias "$alias" \
    --env-file "$env_file" --restart unless-stopped --init --read-only \
    --tmpfs /tmp:rw,nosuid,nodev --security-opt no-new-privileges --cap-drop ALL \
    --memory "$memory" --cpus "$cpus" --volume "$volume:/data" "$image" >/dev/null; then
    restore_previous
    exit 1
fi
ready=false
for _ in $(seq 1 30); do
    status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' "$name")
    if [[ "$status" == healthy ]]; then ready=true; break; fi
    if [[ "$status" == unhealthy ]]; then break; fi
    sleep 3
done
if [[ "$ready" != true ]]; then
    echo 'replacement failed readiness; restoring previous container' >&2
    restore_previous
    exit 1
fi
printf '%s service=%s target=%s image=%s previous=%s result=healthy\n' "$(date -u +%FT%TZ)" "$service" "$target" "$image" "$previous" >> "$state_root/audit.log"
