# currency-server

Prototype data server for the Currency Dashboard (rpi_dash_currency).
Admin edits title/subtitle/palette/effects/currencies; dashboards pull `GET /api/payload`.

    docker compose up -d --build   # or: ./run.sh (no Docker)
    ./run.sh          # first run: creates venv + .env (edit ADMIN_PASSWORD)
    http://HOST:8089/admin        (Basic auth, any username)
    http://HOST:8089/api/payload  (public, or Bearer token if API_TOKEN is set in .env)

Admin also lists the dashboards that pulled in the last hour (by client ID, in memory).

Payload: `{schema:1, version:int, updated_at, settings:{...}, currencies:[{code,name,symbol,price,enabled,flag(url|null)}]}`.
`version` increments on every save. `data.json` is created on first save.

## Docker

    docker compose up -d --build     # after every git pull
    docker compose logs -f
    docker compose down              # keeps data. NEVER use `down -v` (deletes the data volume)

Data (`data.json`, its `.bak`, flags) lives in the named volume `currency-server-data`.

Backup / restore:

    docker run --rm -v currency-server-data:/d -v "$PWD":/b alpine tar czf /b/backup.tgz -C /d .
    docker run --rm -v currency-server-data:/d -v "$PWD":/b alpine tar xzf /b/backup.tgz -C /d

Import existing local data (from `./run.sh`) into the volume:

    docker run --rm -v currency-server-data:/d -v "$PWD":/s alpine sh -c 'cp /s/data.json /d/ && mkdir -p /d/flags && cp /s/static/flags/*.png /d/flags/ 2>/dev/null; chown -R 1000 /d'

Health: `/healthz`. The app also self-checks it and exits after 3 failures, so Docker restarts it.
Admin login uses HTTP Basic auth: put it behind HTTPS (reverse proxy) if it is not on a trusted LAN.
