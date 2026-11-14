# Plots and Prosper backend

The backend is a Django API. The [GitHub Actions workflow](.github/workflows/backend.yml) tests the Docker image on pull requests and `main`. Successful runs on `main` deploy the backend image over SSH. To retry manually, select **Actions → Backend CI and deploy → Run workflow** and choose `main`.

## Set up production

1. Use a Linux `amd64` server with Docker Engine, the Docker Compose v2 plugin, Bash, and `flock` (from `util-linux`). Give the deployment SSH account access to Docker without `sudo`. Password SSH must be reachable from GitHub-hosted runners on port 22.
2. Point the API hostname at the server and make TCP ports 80 and 443 available to the existing Caddy service. Add [the example site block](deploy/Caddyfile) to the server's Caddyfile, replace `api.example.com` with your API hostname, and reload Caddy. The site proxies to `127.0.0.1:8000`. If that port is occupied, set `BACKEND_PORT` in `/etc/plots/production.env` and use the same port in the Caddyfile. Caddy obtains the public certificate and redirects HTTP to HTTPS.
3. Save the configuration on the server at `/etc/plots/production.env`. Run these commands as the deployment SSH user if it has `sudo` access, or have a server administrator create the file with mode `600` and make the SSH user its owner.

   ```bash
   sudo install -d -m 755 /etc/plots
   sudo touch /etc/plots/production.env
   sudo chmod 600 /etc/plots/production.env
   sudo chown "$(id -u):$(id -g)" /etc/plots/production.env
   nano /etc/plots/production.env
   ```

   Use [the example](deploy/production.env.example) to fill in the file. Set `API_HOSTNAME` to the public API hostname without a scheme or port. Generate `DJANGO_SECRET_KEY` once with `openssl rand -hex 32` and keep it between deployments. If you already created a production file, copy its values to the new path. Set `FRONTEND_ORIGIN` to the frontend's full HTTPS origin when available. For multiple allowed origins, use a comma-separated list. Set `NEXT_PUBLIC_API_BASE_URL` to the HTTPS API URL when building the frontend.
4. In the backend repository, create the `production` environment under **Settings → Environments** and restrict deployments to the `main` branch. Add these environment secrets:

   | Secret | Value |
   | --- | --- |
   | `SSH_HOST` | Hostname or IP address reachable by the runner |
   | `SSH_USER` | Deployment account name |
   | `SSH_PASSWORD` | Deployment account password |

   Each deployment job trusts the SSH host key on its first connection and rejects host key changes for the rest of that job.

After the first deployment passes its HTTPS check, create the initial Django admin interactively as the deployment user:

```bash
cd .plotsandprosper
release=$(cat current)
docker compose --project-name plotsandprosper \
  --env-file /etc/plots/production.env --env-file "releases/$release/release.env" \
  -f "releases/$release/compose.yml" exec backend python manage.py createsuperuser
```

The prompt asks for an email address, full name, and password. Sign in at `/admin/` on the API hostname.

## Deployments and data

The first run creates a fresh database. Subsequent runs briefly stop Gunicorn, create a consistent SQLite backup in the `plotsandprosper_database` Docker volume, and migrate that copy. The deploy script checks the replacement on a temporary loopback port before starting it on the port used by Caddy. If migration or the preliminary health check fails, the script starts the prior image on the previous database. A failed public HTTPS check reports a failed workflow while keeping the new database active.

The `current` file identifies the active release. Its database file is `/data/<release>.sqlite3` inside the volume. Release files, images, and previous database snapshots accumulate on the server. Back up the active SQLite database to independent storage and periodically remove old snapshots, release directories, and unused images while preserving the active release and any copies you intend to retain. Never run `docker compose down -v` on production because it removes the SQLite volume.
