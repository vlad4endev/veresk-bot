# AGENTS.md

## Cursor Cloud specific instructions

### What this project is
Single Python app in `veresk_bot/` — the **Veresk** Telegram bot for a flower shop plus a bundled
web admin panel (mailing/CRM), a Telegram Mini App, and multi-channel senders. One process, one
`requirements.txt`, one Dockerfile/compose. There is **no test suite** and **no linter config** in
the repo.

### Toolchain / environment
- Python 3.12 with a virtualenv at `veresk_bot/.venv` (the startup update script recreates and
  refreshes it; system pkg `python3.12-venv` is preinstalled). Activate with
  `source veresk_bot/.venv/bin/activate` or call binaries directly, e.g. `veresk_bot/.venv/bin/python`.
- Redis 7 (`redis-server`) is preinstalled but **not auto-started**. Start it (only needed for the
  full bot, not the admin panel) with: `redis-server --daemonize yes --save "" --appendonly no`.
- Docker is **not** available; run the app natively rather than via `docker compose`.

### Required config: `.env` (gitignored, not committed)
`config.py` calls `sys.exit(1)` at import time if `BOT_TOKEN`, `POSIFLORA_USERNAME`,
`POSIFLORA_PASSWORD`, or `POSIFLORA_STORE_ID` are missing — so **every** entry point (including the
standalone admin panel) needs a `veresk_bot/.env`. For secret-free local dev, placeholder values are
enough because the admin panel never actually calls Telegram/Posiflora. `BOT_TOKEN` must contain a
`:`. Create it once with:
```bash
cd veresk_bot && cp .env.example .env
sed -i 's|^BOT_TOKEN=.*|BOT_TOKEN=111111:PLACEHOLDER_LOCAL_DEV|' .env
sed -i 's|^POSIFLORA_STORE_ID=.*|POSIFLORA_STORE_ID=0|' .env
sed -i 's|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=admin123|' .env
sed -i 's|^DATABASE_PATH=.*|DATABASE_PATH=data/veresk.db|; s|^SESSIONS_DIR=.*|SESSIONS_DIR=data/sessions|' .env
```
Use `DATABASE_PATH=data/veresk.db` (relative) for native runs — the `.env.example` default
`/app/data/...` is the Docker path.

### Running / verifying (do this from `veresk_bot/`)
- **Admin panel only (best secret-free smoke test):** `.venv/bin/python run_admin_local.py`
  serves http://127.0.0.1:3005/admin/ , auto-seeds 2 demo customers, and needs no Redis or real
  secrets. Log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env` (e.g. `admin` / `admin123`).
  Core CRM (clients, events, campaigns) works fully against local SQLite.
- **Full bot (`.venv/bin/python bot.py`)** needs a **real** `BOT_TOKEN` (validated via Telegram
  `getMe` on startup — it will `SystemExit` on a fake token) and Posiflora credentials, plus Redis
  for status polling / FSM. Not runnable without those secrets.
- `bot.py` hardcodes `/app/logs` for its log file, so a native `python bot.py` run needs
  `/app/logs` to be writable (`sudo mkdir -p /app/logs`); `run_admin_local.py` avoids this.

### Lint / test
No configured linter or tests. For a quick sanity check use
`veresk_bot/.venv/bin/python -m compileall -q veresk_bot` (skips `.venv`).
