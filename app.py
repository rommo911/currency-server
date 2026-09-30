#!/usr/bin/env python3
"""Currency data server: admin panel + public JSON payload for dashboards.

GET /api/payload  -> the payload (public, or Bearer token if API_TOKEN is set)
GET /admin        -> edit form (Basic auth); every save bumps `version`
GET /flags/<file> -> uploaded/fetched flag images
"""
import hashlib
import hmac
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
API_TOKEN = os.environ.get("API_TOKEN", "")  # optional: if set, /api/payload needs "Authorization: Bearer <token>"

SCHEMA = 1  # payload format; client rejects other values
PALETTES = ["midnight", "emerald", "sunset", "pearl"]
MAX_TITLE, MAX_SUBTITLE, MAX_NAME, MAX_SYMBOL, MAX_CODE, MAX_PRICE = 80, 140, 25, 3, 5, 1_000_000
MAX_CURRENCIES = 40  # dashboards reject a payload with more, so never publish more
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

DEFAULT = {
    "version": 1,
    "updated_at": 0,
    "settings": {"title": "Prices Dashboard", "subtitle": "Current prices", "color_palette": "midnight",
                 "show_updated_at": True, **{k: True for k in FX}},
    "currencies": [
        {"code": "USD", "name": "US Dollar", "symbol": "$", "price": 1, "flag": None, "enabled": True},
        {"code": "EUR", "name": "Euro", "symbol": "€", "price": 1, "flag": None, "enabled": True},
    ],
}


def _normalize(d):
    """Tolerate files from older/newer versions: fill in anything missing."""
    d = d if isinstance(d, dict) else {}
    d["version"] = d["version"] if isinstance(d.get("version"), int) else 1
    d.setdefault("updated_at", 0)
    d["settings"] = {**DEFAULT["settings"], **(d.get("settings") or {})}
    d.setdefault("currencies", [])
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


def parse_price(raw):
    """Whole numbers only."""
    raw = (raw or "").strip()
    if not re.fullmatch(r"\d{1,8}", raw):
        return None
    v = int(raw)
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
    now = time.time()
    with _clients_lock:
        if len(clients) >= MAX_CLIENTS and key not in clients:
            for k in [k for k, c in clients.items() if now - c["last_seen"] > CLIENT_WINDOW] or [min(clients, key=lambda k: clients[k]["last_seen"])]:
                del clients[k]
        c = clients.setdefault(key, {"first_seen": now, "pulls": 0})
        c.update(name=name or "?", ip=ip, version=version, last_seen=now)
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
    resp = jsonify({
        "schema": SCHEMA,
        "version": d["version"],
        "updated_at": d["updated_at"],
        "settings": d["settings"],
        "currencies": [
            {**{k: c[k] for k in ("code", "name", "symbol", "enabled")},
             "price": int(round(c["price"])),
             "flag": f"{base}/flags/{c['flag']}" if c.get("flag") else None}
            for c in d["currencies"]
        ],
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
    return render_template("admin.html", d=d, clients=recent_clients(), ago_text=ago_text, s=d["settings"], palettes=PALETTES, csrf=CSRF,
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

    title = clean_text(f.get("title"), MAX_TITLE)
    subtitle = clean_text(f.get("subtitle"), MAX_SUBTITLE)
    if not title:
        return fail(f"Title is required (max {MAX_TITLE} chars)")
    if subtitle is None:
        return fail(f"Subtitle too long (max {MAX_SUBTITLE})")
    palette = f.get("color_palette")
    if palette not in PALETTES:
        return fail("Invalid palette")

    currencies, seen = [], set()
    try:
        rows = min(max(int(f.get("rows", "0")), 0), 200)
    except ValueError:
        return fail("Bad form data, reload the page")
    for i in range(rows + 1):  # +1 = the blank "add" row
        code = re.sub(r"[^A-Za-z0-9]", "", f.get(f"code_{i}", "")).upper()[:MAX_CODE]
        if not code or f.get(f"remove_{i}"):
            continue
        if code in seen:
            return fail(f"Duplicate code {code}")
        seen.add(code)
        name = clean_text(f.get(f"name_{i}"), MAX_NAME)
        symbol = clean_text(f.get(f"symbol_{i}"), MAX_SYMBOL)
        price = parse_price(f.get(f"price_{i}"))
        if not name or symbol is None:
            return fail(f"{code}: name required (max {MAX_NAME}), symbol max {MAX_SYMBOL}")
        if price is None:
            return fail(f"{code}: invalid price (whole number 0..{MAX_PRICE:,})")
        flag = f.get(f"flag_{i}") or None
        if flag and not re.fullmatch(r"[a-z0-9]+\.png", flag):
            flag = None
        up = request.files.get(f"upload_{i}")
        if up and up.filename:
            body = up.read(500_001)
            if not body.startswith(b"\x89PNG") or len(body) > 500_000:
                return fail(f"{code}: flag must be a PNG under 500 KB")
            flag = f"{code.lower()}.png"
            with open(os.path.join(FLAGS_DIR, flag), "wb") as out:
                out.write(body)
        elif not flag:
            flag = flag_for(code)
        currencies.append({"code": code, "name": name, "symbol": symbol, "price": price,
                           "flag": flag, "enabled": bool(f.get(f"enabled_{i}"))})

    if len(currencies) > MAX_CURRENCIES:
        return fail(f"Too many currencies (max {MAX_CURRENCIES})")

    with _lock:
        d = load()
        d["settings"] = {"title": title, "subtitle": subtitle, "color_palette": palette,
                         "show_updated_at": bool(f.get("show_updated_at")), **{k: bool(f.get(k)) for k in FX}}
        d["currencies"] = currencies
        save(d)
    return redirect(url_for("admin", msg=f"Saved. Payload version is now {d['version']}"))


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


if __name__ == "__main__":
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        from waitress import serve  # production server (in requirements.txt)
        serve(app, host="0.0.0.0", port=PORT, threads=8)
    except ImportError:  # e.g. an old ./run.sh venv without waitress
        app.run(host="0.0.0.0", port=PORT, threaded=True)
