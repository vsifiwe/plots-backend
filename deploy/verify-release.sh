#!/usr/bin/env bash
set -Eeuo pipefail

sha=$1
root=$(mktemp -d)
root=$(cd "$root" && pwd -P)
export COMPOSE_PROJECT_NAME="plotsandprosper-test-$$"

first=$(openssl rand -hex 20)
second=$(openssl rand -hex 20)
broken=$(openssl rand -hex 20)
base_image="plotsandprosper-backend:$sha"

compose_current() {
    local current
    current=$(cat "$root/current")
    docker compose --project-name "$COMPOSE_PROJECT_NAME" \
        --env-file "$root/production.env" --env-file "$root/releases/$current/release.env" \
        -f "$root/releases/$current/compose.yml" "$@"
}

cleanup() {
    local release_dir
    if [[ -f $root/current ]]; then
        compose_current down -v >/dev/null || true
    elif [[ -n ${bundle:-} ]]; then
        APP_IMAGE="$base_image" DJANGO_DB_PATH=/data/test.sqlite3 \
            docker compose --project-name "$COMPOSE_PROJECT_NAME" \
                --env-file "$root/production.env" -f "$bundle/compose.yml" down -v >/dev/null || true
    fi
    docker image rm "plotsandprosper-backend:$first" "plotsandprosper-backend:$second" \
        "plotsandprosper-backend:$broken" >/dev/null 2>&1 || true
    for release_dir in "$root"/releases/*/; do
        if [[ -d $release_dir ]]; then
            docker image rm "plotsandprosper-backend:$(basename "$release_dir")" >/dev/null 2>&1 || true
        fi
    done
    rm -rf -- "$root"
}
trap cleanup EXIT

cat > "$root/production.env" <<'EOF'
API_HOSTNAME=localhost
DJANGO_SECRET_KEY=ci-only-django-secret-key-NzajbpeYU1Gj6pQGpZD6yqeSYaQ7wbD
HTTP_BIND=127.0.0.1:
HTTPS_BIND=127.0.0.1:
EOF

package() {
    bundle=$(mktemp -d "$root/incoming.XXXXXXXX")
    cp deploy/Caddyfile deploy/compose.yml "$bundle/"
    docker image save "plotsandprosper-backend:$1" | gzip > "$bundle/backend.tar.gz"
}

check_https() {
    local address attempt
    address=$(compose_current port caddy 443)
    # Use Caddy's disposable local CA to verify HTTPS without public DNS.
    for ((attempt = 0; attempt < 15; attempt++)); do
        if compose_current exec -T caddy cat /data/caddy/pki/authorities/local/root.crt \
            > "$root/root.crt" 2>/dev/null && [[ -s $root/root.crt ]]; then
            break
        fi
        sleep 1
    done
    test -s "$root/root.crt"
    curl --fail --silent --show-error --retry 15 --retry-all-errors --retry-delay 1 --max-time 5 \
        --cacert "$root/root.crt" "https://localhost:${address##*:}/healthz/"
    curl --fail --silent --show-error --max-time 5 --cacert "$root/root.crt" \
        "https://localhost:${address##*:}/admin/login/" > /dev/null
    curl --fail --silent --show-error --max-time 5 --cacert "$root/root.crt" \
        "https://localhost:${address##*:}/static/admin/css/base.css" > /dev/null
}

docker tag "$base_image" "plotsandprosper-backend:$first"
package "$first"
bash deploy/release.sh "$root" "$first" "$bundle"
check_https
compose_current exec -T backend python manage.py shell -c \
    'from app.models import Member; Member.objects.create_user(email="deployment-test@example.com", full_name="Deployment Test", password="ci-only-password")'

docker tag "$base_image" "plotsandprosper-backend:$second"
package "$second"
bash deploy/release.sh "$root" "$second" "$bundle"
check_https
stable_release=$(cat "$root/current")

check_record() {
    compose_current exec -T backend python manage.py shell -c \
        'from app.models import Member; Member.objects.get(email="deployment-test@example.com")'
}
check_record

# Migrate the next copy normally, then fail its HTTP health check.
docker build --tag "plotsandprosper-backend:$broken" - <<EOF
FROM $base_image
CMD ["python", "-c", "import time; time.sleep(120)"]
EOF
package "$broken"
echo 'The following release is expected to fail its internal health check.'
if bash deploy/release.sh "$root" "$broken" "$bundle" 2> "$root/failed-release.log"; then
    echo 'The intentionally unhealthy release was accepted.' >&2
    exit 1
fi
cat "$root/failed-release.log" >&2
if [[ $(cat "$root/current") != "$stable_release" ]] ||
    ! grep -Fq "Restoring $stable_release" "$root/failed-release.log"; then
    echo 'The failure did not restore the prior release.' >&2
    exit 1
fi
check_https
check_record
