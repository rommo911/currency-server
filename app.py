#!/usr/bin/env python3
"""Currency data server: admin panel + public JSON payload for dashboards.

GET /api/payload  -> the payload (public, no auth)
GET /admin        -> edit form (Basic auth); every save bumps `version`
GET /flags/<file> -> uploaded/fetched flag images
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import unicodedata
import urllib.request

from flask import Flask, Response, jsonify, redirect, render_template, request, send_from_directory, url_for

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(APP_DIR, "data.json")
FLAGS_DIR = os.path.join(APP_DIR, "static", "flags")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD is not set (see .env.example)")
PORT = int(os.environ.get("PORT", "8089"))
API_TOKEN = os.environ.get("API_TOKEN", "")  # optional: if set, /api/payload needs "Authorization: Bearer <token>"

SCHEMA = 1  # payload format; client rejects other values
PALETTES = ["midnight", "emerald", "sunset", "pearl"]
MAX_TITLE, MAX_SUBTITLE, MAX_NAME, MAX_SYMBOL, MAX_CODE, MAX_PRICE = 80, 140, 25, 3, 5, 1_000_000
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


def load():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return json.loads(json.dumps(DEFAULT))


def save(data):
    data["version"] += 1
    data["updated_at"] = int(time.time())
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)  # atomic: readers never see a half-written file


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


def auth_ok():
    a = request.authorization
    return bool(a) and hmac.compare_digest(a.password or "", ADMIN_PASSWORD)


def deny():
    return Response("Authentication required.", 401, {"WWW-Authenticate": 'Basic realm="Currency Server"'})


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
    for i in range(int(f.get("rows", "0")) + 1):  # +1 = the blank "add" row
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

    with _lock:
        d = load()
        d["settings"] = {"title": title, "subtitle": subtitle, "color_palette": palette,
                         "show_updated_at": bool(f.get("show_updated_at")), **{k: bool(f.get(k)) for k in FX}}
        d["currencies"] = currencies
        save(d)
    return redirect(url_for("admin", msg=f"Saved. Payload version is now {d['version']}"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
