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
