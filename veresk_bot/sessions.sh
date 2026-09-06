#!/usr/bin/env bash
# Надёжная работа с сессиями Telegram / MAX.
#
# Пока bot крутится — heal идёт через его HTTP API (второй Telethon
# на тот же .session отозвал бы вход). Скрипт всегда копируется в контейнер,
# чтобы не зависеть от последней пересборки образа.
#
#   ./sessions.sh              # doctor
#   ./sessions.sh doctor
#   ./sessions.sh heal
#   ./sessions.sh recover --dry-run
#   ./sessions.sh dedupe --dry-run
#   ./sessions.sh watch --interval 180
set -euo pipefail
cd "$(dirname "$0")"

if [[ $# -eq 0 ]]; then
  set -- doctor
fi

if command -v docker >/dev/null 2>&1 && [[ -f docker-compose.yml ]]; then
  if docker compose ps --status running --services 2>/dev/null | grep -qx bot; then
    docker compose cp session_doctor.py bot:/app/session_doctor.py 2>/dev/null || true
    exec docker compose exec -T bot python session_doctor.py "$@"
  fi
fi

exec python3 session_doctor.py "$@"
