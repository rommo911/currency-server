# currency-server

Prototype data server for the Currency Dashboard (rpi_dash_currency).
Admin edits title/subtitle/palette/effects/currencies; dashboards pull `GET /api/payload`.

    ./run.sh          # first run: creates venv + .env (edit ADMIN_PASSWORD)
    http://HOST:8080/admin        (Basic auth, any username)
    http://HOST:8080/api/payload  (public)

Payload: `{schema:1, version:int, updated_at, settings:{...}, currencies:[{code,name,symbol,price,enabled,flag(url|null)}]}`.
`version` increments on every save. `data.json` is created on first save.
