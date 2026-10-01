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

## Workspace

`dashboard/` is a git submodule (rpi_dash_currency, branch `centralized-admin`).

    git clone --recurse-submodules https://github.com/rommo911/currency-server.git
    git submodule update --init      # in an existing clone
    git submodule update --remote    # pull the latest dashboard

## Docker

    mkdir -p data                    # once (must be owned by uid 1000, i.e. normally you)
    docker compose up -d --build     # after every git pull
    docker compose logs -f
    docker compose down              # safe: data is in ./data, not in the container

Settings live in the plain local folder `./data/` (`data.json`, its `.bak`, `flags/`); it is
gitignored, so `git pull` never touches it. Back it up with `cp -a data data.backup`.
If the log says `NOTICE: no /data/data.json yet`, the server started with defaults.

Import existing data:

    cp data.json data/                                       # from a previous ./run.sh
    mkdir -p data/flags && cp static/flags/*.png data/flags/ 2>/dev/null
    # from the old Docker volume (versions before 0.3.0):
    docker run --rm -v currency-server_currency-data:/from -v "$PWD/data":/to alpine cp -a /from/. /to/

Health: `/healthz`. The app also self-checks it and exits after 3 failures, so Docker restarts it.
Admin login uses HTTP Basic auth: put it behind HTTPS (reverse proxy) if it is not on a trusted LAN.
Version: see `VERSION` (shown in the admin page and as `server_version` in the payload).
