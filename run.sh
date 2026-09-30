#!/bin/sh
# Prototype launcher: creates venv on first run, loads .env, starts server.
cd "$(dirname "$0")" || exit 1
[ -f .env ] || cp .env.example .env
[ -d venv ] || { python3 -m venv venv && venv/bin/pip install -q -r requirements.txt; }
set -a; . ./.env; set +a
exec venv/bin/python app.py
