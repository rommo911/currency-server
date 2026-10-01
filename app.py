#!/usr/bin/env python3
"""Currency data server: admin panel + public JSON payload for dashboards.

GET /api/payload  -> the payload (public, or Bearer token if API_TOKEN is set)
GET /admin        -> edit form (Basic auth); every save bumps `version`
GET /flags/<file> -> uploaded/fetched flag images
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import unicodedata
import urllib.request

from flask import Flask, Response, jsonify, redirect, render_template, request, send_from_directory, url_for

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR (used by Docker) moves data.json and flags onto a volume; unset = next to the code.
DATA_DIR = os.environ.get("DATA_DIR", "")
DATA_FILE = os.path.join(DATA_DIR or APP_DIR, "data.json")
FLAGS_DIR = os.path.join(DATA_DIR, "flags") if DATA_DIR else os.path.join(APP_DIR, "static", "flags")
os.makedirs(FLAGS_DIR, exist_ok=True)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD is not set (see .env.example)")
if ADMIN_PASSWORD == "changeme123":
    print("WARNING: ADMIN_PASSWORD is still the example value - change it in .env", file=sys.stderr)
PORT = int(os.environ.get("PORT", "8089"))
try:
    with open(os.path.join(APP_DIR, "VERSION"), encoding="utf-8") as _f:
        VERSION = _f.read().strip() or "unknown"
except OSError:
    VERSION = "unknown"
if not os.path.exists(DATA_FILE):
    print(f"NOTICE: no {DATA_FILE} yet - starting with default settings (first run, or the data folder/volume is not the one you expect)", file=sys.stderr, flush=True)
API_TOKEN = os.environ.get("API_TOKEN", "")  # optional: if set, /api/payload needs "Authorization: Bearer <token>"

SCHEMA = 1  # payload format; client rejects other values
PALETTES = ["midnight", "emerald", "sunset", "pearl"]
MAX_TITLE, MAX_SUBTITLE, MAX_NAME, MAX_SYMBOL, MAX_CODE, MAX_PRICE = 80, 140, 25, 3, 5, 1_000_000
ROW_MIN, ROW_MAX = 2, 4  # currencies per enabled row (dashboards show at most 4 per row)
ROWS = 2
MAX_CLIENTS = 1000  # cap on the in-memory client list
ADMIN_MAX_FAILURES, ADMIN_LOCK_WINDOW = 5, 300
WATCHDOG_INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "30"))
FX = ["fx_glass", "fx_scan", "fx_flash", "fx_glow"]

CLIENT_WINDOW = 3600  # seconds a client stays listed after its last pull
clients = {}  # client id -> {name, ip, version, first_seen, last_seen, pulls}; in memory, resets on restart
_clients_lock = threading.Lock()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
_lock = threading.Lock()
CSRF = hmac.new(secrets.token_bytes(32), b"csrf", hashlib.sha256).hexdigest()

def _default_currencies():
    return [
        {"code": "USD", "name": "US Dollar", "symbol": "$", "price": 1, "flag": None, "enabled": True},
        {"code": "EUR", "name": "Euro", "symbol": "€", "price": 1, "flag": None, "enabled": True},
        {"code": "GBP", "name": "Pound", "symbol": "£", "price": 1, "flag": None, "enabled": True},
    ]


DEFAULT = {
    "version": 1,
    "updated_at": 0,
    "settings": {"title": "Prices Dashboard", "subtitle": "Current prices", "color_palette": "midnight",
                 "show_updated_at": True, **{k: True for k in FX}},
    "rows": [
        {"enabled": True, "title": "Prices Dashboard", "subtitle": "Current prices", "currencies": _default_currencies()},
        {"enabled": False, "title": "", "subtitle": "", "currencies": _default_currencies()},
    ],
}


def _norm_currency(c):
    if not isinstance(c, dict) or not c.get("code"):
        return None
    try:
        price = norm_price(c.get("price", 0))
    except (TypeError, ValueError, OverflowError):
        price = 0
    return {"code": str(c["code"]), "name": str(c.get("name") or c["code"]), "symbol": str(c.get("symbol") or ""),
            "price": price, "flag": c.get("flag") or None, "enabled": bool(c.get("enabled", True))}


def _norm_row(r):
    r = r if isinstance(r, dict) else {}
    cur = [x for x in (_norm_currency(c) for c in (r.get("currencies") or [])) if x]
    return {"enabled": bool(r.get("enabled", False)), "title": str(r.get("title") or ""),
            "subtitle": str(r.get("subtitle") or ""), "currencies": cur}


def mirror_legacy(d):
    """Old dashboards read settings.title/subtitle + flat `currencies`: keep them equal to the first enabled row."""
    first = next((r for r in d["rows"] if r["enabled"]), d["rows"][0])
    d["settings"]["title"], d["settings"]["subtitle"] = first["title"], first["subtitle"]
    d["currencies"] = first["currencies"]


def _normalize(d):
    """Tolerate files from older/newer versions: fill in anything missing. Files without `rows` are migrated."""
    d = d if isinstance(d, dict) else {}
    d["version"] = d["version"] if isinstance(d.get("version"), int) else 1
    d.setdefault("updated_at", 0)
    d["settings"] = {**DEFAULT["settings"], **(d.get("settings") or {})}
    if not isinstance(d.get("rows"), list) or not d["rows"]:
        legacy = {"enabled": True, "title": d["settings"]["title"], "subtitle": d["settings"]["subtitle"],
                  "currencies": d.get("currencies") or []}
        # Row 2 starts as a disabled copy of row 1 (as many cards as fit), ready to edit.
        second = json.loads(json.dumps(legacy))
        second["enabled"] = False
        second["currencies"] = second["currencies"][:ROW_MAX]
        d["rows"] = [legacy, second]
    d["rows"] = [_norm_row(r) for r in d["rows"][:ROWS]]
    while len(d["rows"]) < ROWS:
        d["rows"].append(_norm_row({}))
    if not any(r["enabled"] for r in d["rows"]):
        d["rows"][0]["enabled"] = True
    mirror_legacy(d)
    return d


def load():
    """data.json -> data.json.bak -> defaults. A corrupt file is kept aside, never overwritten silently."""
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return _normalize(json.load(f))
    except FileNotFoundError:
        return _normalize(json.loads(json.dumps(DEFAULT)))  # first run
    except (OSError, ValueError) as e:
        print(f"ERROR: cannot read {DATA_FILE}: {e}", file=sys.stderr)
    try:
        with open(DATA_FILE + ".bak", encoding="utf-8") as f:
            print("Recovered from data.json.bak", file=sys.stderr)
            return _normalize(json.load(f))
    except (OSError, ValueError):
        pass
    try:
        shutil.copyfile(DATA_FILE, f"{DATA_FILE}.corrupt-{int(time.time())}")
    except OSError:
        pass
    d = _normalize(json.loads(json.dumps(DEFAULT)))
    d["version"] = int(time.time())  # keep versions increasing so dashboards accept the reset
    return d


def save(data):
    mirror_legacy(data)
    data["version"] += 1
    data["updated_at"] = int(time.time())
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(DATA_FILE):
            shutil.copyfile(DATA_FILE, DATA_FILE + ".bak")  # last known good
        os.replace(tmp, DATA_FILE)  # atomic: readers never see a half-written file
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def clean_text(raw, max_len):
    """None if too long."""
    s = "".join(c for c in (raw or "") if c == " " or unicodedata.category(c)[0] != "C")
    s = re.sub(r"\s+", " ", s).strip()
    return None if len(s) > max_len else s


def norm_price(v):
    """Round to 2 decimals; a whole value stays an int so it prints without ".0"."""
    v = round(float(v), 2)
    return int(v) if v.is_integer() else v


def parse_price(raw):
    """Non-negative number with at most 2 decimals."""
    raw = (raw or "").strip()
    if not re.fullmatch(r"\d{1,8}(\.\d{1,2})?", raw):
        return None
    v = norm_price(raw)
    return v if v <= MAX_PRICE else None


def flag_for(code):
    """Best-effort flag download from flagcdn; filename or None."""
    cc = {"EUR": "eu"}.get(code, code[:2].lower())
    try:
        req = urllib.request.Request(f"https://flagcdn.com/w320/{cc}.png", headers={"User-Agent": "currency-server"})
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read(500_000)
        if not body.startswith(b"\x89PNG"):
            return None
        name = f"{code.lower()}.png"
        with open(os.path.join(FLAGS_DIR, name), "wb") as f:
            f.write(body)
        return name
    except Exception:  # noqa: BLE001 - any failure just means no flag
        return None


def record_client():
    """Count this pull. Key is the client's UUID; older clients without one are keyed by IP."""
    ip = request.remote_addr or "?"
    cid = re.sub(r"[^A-Za-z0-9-]", "", request.headers.get("X-Client-Id", ""))[:40]
    key = cid or f"ip:{ip}"
    name = clean_text(request.headers.get("X-Client-Name"), 40) or ("unknown (old client)" if not cid else "?")
    version = clean_text(request.headers.get("X-Client-Version"), 30) or "?"
    try:  # self-reported LAN address (informational only): keep it only if it is a real IP
        local_ip = str(ipaddress.ip_address((request.headers.get("X-Client-IP") or "").strip()))
    except ValueError:
        local_ip = "-"
    now = time.time()
    with _clients_lock:
        if len(clients) >= MAX_CLIENTS and key not in clients:
            for k in [k for k, c in clients.items() if now - c["last_seen"] > CLIENT_WINDOW] or [min(clients, key=lambda k: clients[k]["last_seen"])]:
                del clients[k]
        c = clients.setdefault(key, {"first_seen": now, "pulls": 0})
        c.update(name=name or "?", ip=ip, local_ip=local_ip, version=version, last_seen=now)
        c["pulls"] += 1


def recent_clients():
    now = time.time()
    with _clients_lock:
        for k in [k for k, c in clients.items() if now - c["last_seen"] > CLIENT_WINDOW]:
            del clients[k]
        rows = [dict(c, id=k if not k.startswith("ip:") else "-", ago=int(now - c["last_seen"])) for k, c in clients.items()]
    return sorted(rows, key=lambda r: r["ago"])


def ago_text(s):
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60}s"


_fails = {}  # ip -> failure timestamps (in memory)
_fails_lock = threading.Lock()


def auth_ok():
    """True if admin credentials are valid. Repeated failures from one IP are locked out for a while."""
    ip, now = request.remote_addr or "?", time.time()
    with _fails_lock:
        recent = [t for t in _fails.get(ip, []) if now - t < ADMIN_LOCK_WINDOW]
        _fails[ip] = recent
        if len(recent) >= ADMIN_MAX_FAILURES:
            return False
    a = request.authorization
    if a and hmac.compare_digest(a.password or "", ADMIN_PASSWORD):
        return True
    with _fails_lock:
        _fails.setdefault(ip, []).append(now)
        if len(_fails) > 5000:  # bound memory
            _fails.clear()
    return False


def deny():
    ip, now = request.remote_addr or "?", time.time()
    with _fails_lock:
        locked = len([t for t in _fails.get(ip, []) if now - t < ADMIN_LOCK_WINDOW]) >= ADMIN_MAX_FAILURES
    if locked:
        return Response("Too many failed logins. Try again in a few minutes.", 429, {"Retry-After": str(ADMIN_LOCK_WINDOW)})
    return Response("Authentication required.", 401, {"WWW-Authenticate": 'Basic realm="Currency Server"'})


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; frame-ancestors 'none'"
    return resp


@app.route("/healthz")
def healthz():
    """Liveness for Docker and the watchdog: data readable and the data dir writable. Not counted as a client."""
    if not os.access(os.path.dirname(DATA_FILE), os.W_OK):
        return Response("data dir not writable", 500)
    try:
        load()
    except Exception as e:  # noqa: BLE001
        return Response(f"data error: {e}", 500)
    return Response("ok", 200, {"Cache-Control": "no-store"})


@app.route("/api/payload")
def payload():
    if API_TOKEN:
        a = request.headers.get("Authorization", "")
        if not a.startswith("Bearer ") or not hmac.compare_digest(a[7:], API_TOKEN):
            return Response("Invalid or missing token.", 401)
    record_client()
    d = load()
    base = request.url_root.rstrip("/")

    def pub(c, legacy=False):
        # The flat list is what dashboards from before decimals read, and they
        # reject non-integers: round it for them, `rows` carries the exact price.
        return {**{k: c[k] for k in ("code", "name", "symbol", "enabled")},
                "price": int(round(c["price"])) if legacy else c["price"],
                "flag": f"{base}/flags/{c['flag']}" if c.get("flag") else None}

    resp = jsonify({
        "schema": SCHEMA,
        "server_version": VERSION,
        "version": d["version"],
        "updated_at": d["updated_at"],
        "settings": d["settings"],  # title/subtitle = first enabled row (what old dashboards show)
        "currencies": [pub(c, True) for c in d["currencies"]],  # legacy flat list = first enabled row
        "rows": [{"enabled": r["enabled"], "title": r["title"], "subtitle": r["subtitle"],
                  "currencies": [pub(c) for c in r["currencies"][:ROW_MAX]]} for r in d["rows"]],
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/flags/<path:name>")
def flags(name):
    return send_from_directory(FLAGS_DIR, name)


@app.route("/")
def root():
    return redirect(url_for("admin"))


@app.route("/admin", methods=["GET"])
def admin():
    if not auth_ok():
        return deny()
    d = load()
    return render_template("admin.html", d=d, clients=recent_clients(), ago_text=ago_text, s=d["settings"], row_max=ROW_MAX, server_version=VERSION, palettes=PALETTES, csrf=CSRF,
                           msg=request.args.get("msg"), error=request.args.get("error"),
                           lim=dict(title=MAX_TITLE, subtitle=MAX_SUBTITLE, name=MAX_NAME, symbol=MAX_SYMBOL, code=MAX_CODE))


@app.route("/admin/save", methods=["POST"])
def admin_save():
    if not auth_ok():
        return deny()
    if not hmac.compare_digest(request.form.get("csrf", ""), CSRF):
        return redirect(url_for("admin", error="CSRF check failed, reload the page"))
    f = request.form

    def fail(msg):
        return redirect(url_for("admin", error=msg))

    palette = f.get("color_palette")
    if palette not in PALETTES:
        return fail("Invalid palette")

    # "r<row>_<index>_up|down" from a card's arrow button: swap that card with its neighbour.
    mv = re.fullmatch(r"r(\d+)_(\d+)_(up|down)", f.get("move", ""))
    rows = []
    for n in range(ROWS):
        row, err = parse_row(f, n, (int(mv.group(2)), mv.group(3)) if mv and int(mv.group(1)) == n else None)
        if err:
            return fail(f"Row {n + 1}: {err}")
        rows.append(row)
    if not any(r["enabled"] for r in rows):
        return fail("Enable at least one row")

    with _lock:
        d = load()
        d["settings"] = {**d["settings"], "color_palette": palette,
                         "show_updated_at": bool(f.get("show_updated_at")), **{k: bool(f.get(k)) for k in FX}}
        d["rows"] = rows
        save(d)
    return redirect(url_for("admin", msg=f"{'Moved and saved' if mv else 'Saved'}. Payload version is now {d['version']}"))


def parse_row(f, n, move=None):
    """Validate form fields r{n}_* -> (row, None) or (None, error message).
    move = (form index, "up"|"down") swaps that card with its neighbour."""
    p = f"r{n}_"
    title = clean_text(f.get(p + "title"), MAX_TITLE)
    subtitle = clean_text(f.get(p + "subtitle"), MAX_SUBTITLE)
    enabled = bool(f.get(p + "enabled"))
    if title is None:
        return None, f"title too long (max {MAX_TITLE})"
    if subtitle is None:
        return None, f"subtitle too long (max {MAX_SUBTITLE})"
    if enabled and not title:
        return None, f"title is required when the row is enabled (max {MAX_TITLE} chars)"
    currencies, seen, form_idx = [], set(), []
    try:
        count = min(max(int(f.get(p + "rows", "0")), 0), 200)
    except ValueError:
        return None, "bad form data, reload the page"
    for i in range(count + 1):  # +1 = the blank "add" row
        code = re.sub(r"[^A-Za-z0-9]", "", f.get(f"{p}code_{i}", "")).upper()[:MAX_CODE]
        if not code or f.get(f"{p}remove_{i}"):
            continue
        if code in seen:
            return None, f"duplicate code {code}"
        seen.add(code)
        name = clean_text(f.get(f"{p}name_{i}"), MAX_NAME)
        symbol = clean_text(f.get(f"{p}symbol_{i}"), MAX_SYMBOL)
        price = parse_price(f.get(f"{p}price_{i}"))
        if not name or symbol is None:
            return None, f"{code}: name required (max {MAX_NAME}), symbol max {MAX_SYMBOL}"
        if price is None:
            return None, f"{code}: invalid price (number 0..{MAX_PRICE:,}, up to 2 decimals)"
        flag = f.get(f"{p}flag_{i}") or None
        if flag and not re.fullmatch(r"[a-z0-9]+\.png", flag):
            flag = None
        up = request.files.get(f"{p}upload_{i}")
        if up and up.filename:
            body = up.read(500_001)
            if not body.startswith(b"\x89PNG") or len(body) > 500_000:
                return None, f"{code}: flag must be a PNG under 500 KB"
            flag = f"{code.lower()}.png"
            with open(os.path.join(FLAGS_DIR, flag), "wb") as out:
                out.write(body)
        elif not flag:
            flag = flag_for(code)
        currencies.append({"code": code, "name": name, "symbol": symbol, "price": price,
                           "flag": flag, "enabled": bool(f.get(f"{p}enabled_{i}"))})
        form_idx.append(i)
    if move and move[0] in form_idx:
        k = form_idx.index(move[0])
        j = k - 1 if move[1] == "up" else k + 1
        if 0 <= j < len(currencies):
            currencies[k], currencies[j] = currencies[j], currencies[k]
    if len(currencies) > ROW_MAX:
        return None, f"at most {ROW_MAX} currencies"
    if enabled and len(currencies) < ROW_MIN:
        return None, f"an enabled row needs at least {ROW_MIN} currencies"
    return {"enabled": enabled, "title": title, "subtitle": subtitle, "currencies": currencies}, None


def watchdog():
    """Self-check /healthz; after 3 failures in a row exit so Docker (restart: unless-stopped) restarts us."""
    fails = 0
    while True:
        time.sleep(WATCHDOG_INTERVAL)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=5).read()
            fails = 0
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"watchdog: health check failed ({fails}/3): {e}", file=sys.stderr, flush=True)
            if fails >= 3:
                os._exit(1)


def lan_ip():
    """Best-effort LAN address (UDP connect sends nothing); None if unknown."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    ip = lan_ip()
    print(f"Currency server v{VERSION} listening on port {PORT}", flush=True)
    print(f"  admin:   http://localhost:{PORT}/admin" + (f"   or   http://{ip}:{PORT}/admin" if ip else ""), flush=True)
    print(f"  payload: http://localhost:{PORT}/api/payload" + (" (Bearer token required)" if API_TOKEN else ""), flush=True)
    print(f"  data:    {DATA_FILE}", flush=True)
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        from waitress import serve  # production server (in requirements.txt)
        serve(app, host="0.0.0.0", port=PORT, threads=8)
    except ImportError:  # e.g. an old ./run.sh venv without waitress
        app.run(host="0.0.0.0", port=PORT, threaded=True)
