#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

root=$(cd "$1" && pwd)
sha=$2
bundle=$(cd "$3" && pwd)
production_env=$4
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid commit SHA' >&2; exit 1; }

# Reserve standard output for the health URL returned to the GitHub runner.
exec 3>&1 1>&2
exec 9>"$root/deploy.lock"
flock -x 9

release_name=$(basename "$bundle")
[[ $release_name =~ ^incoming\.[a-zA-Z0-9]+$ ]] || { echo 'Invalid bundle directory'; exit 1; }
release_name="$sha.${release_name#incoming.}"
release="$root/releases/$release_name"
database="/data/$release_name.sqlite3"
previous=''
if [[ -f "$root/current" ]]; then
    IFS= read -r previous < "$root/current"
    [[ $previous =~ ^[0-9a-f]{40}\.[a-zA-Z0-9]+$ ]] || { echo 'Invalid current release'; exit 1; }
fi

compose() {
    local selected=$1
    shift
    docker compose --project-name "${COMPOSE_PROJECT_NAME:-plotsandprosper}" \
        --env-file "$production_env" --env-file "$root/releases/$selected/release.env" \
        -f "$root/releases/$selected/compose.yml" "$@"
}

start_backend() {
    compose "$1" up -d --wait --wait-timeout 60 backend
}

runtime_replaced=false
recover() {
    local status=$?
    if [[ $status -eq 0 ]] || { [[ -f "$root/current" ]] && [[ $(cat "$root/current") == "$release_name" ]]; }; then
        return
    fi
    trap - EXIT HUP INT TERM
    set +e
    if [[ $runtime_replaced == true ]]; then
        compose "$release_name" logs --tail 30 backend
        if ! compose "$release_name" stop caddy backend; then
            echo 'Could not stop the failed release. Inspect Compose before restoring service.'
            exit "$status"
        fi
        if [[ -n $previous ]]; then
            echo "Restoring $previous"
            if ! start_backend "$previous" || ! compose "$previous" up -d caddy; then
                echo 'Automatic recovery failed. Inspect Compose and the release in current.'
            fi
        fi
    fi
    exit "$status"
}
trap recover EXIT
trap 'exit 1' HUP INT TERM

mkdir -p "$root/releases"
mkdir "$release"
cp "$bundle/compose.yml" "$bundle/Caddyfile" "$release/"
printf 'APP_IMAGE=plotsandprosper-backend:%s\nDJANGO_DB_PATH=%s\n' "$release_name" "$database" > "$release/release.env"

docker image load -i "$bundle/backend.tar.gz"
# A retry of the same commit can rebuild its SHA tag. Keep the prior image addressable.
docker image tag "plotsandprosper-backend:$sha" "plotsandprosper-backend:$release_name"
compose "$release_name" config --quiet
compose "$release_name" pull --policy missing caddy
compose "$release_name" run --rm -T --no-deps caddy caddy validate --config /etc/caddy/Caddyfile
hostname=$(compose "$release_name" run --rm -T --no-deps backend python -c \
    'from core.production import API_HOSTNAME; print(API_HOSTNAME)')

runtime_replaced=true
compose "${previous:-$release_name}" stop caddy backend
if [[ -n $previous ]]; then
    compose "$release_name" run --rm -T --no-deps backend \
        python deploy/database.py "/data/$previous.sqlite3" "$database"
fi
compose "$release_name" run --rm -T --no-deps backend python manage.py migrate --noinput
start_backend "$release_name"

# Once the new database can receive public writes, it must remain the live one.
printf '%s\n' "$release_name" > "$root/current.next"
mv -f "$root/current.next" "$root/current"
compose "$release_name" up -d caddy
printf 'https://%s/healthz/\n' "$hostname" >&3
