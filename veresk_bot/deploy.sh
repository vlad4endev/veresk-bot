#!/usr/bin/env bash
# Обновление admin.veresk-flowers.ru из git + пересборка Docker.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> git pull"
git pull --ff-only

echo "==> очистка битых MAX-сессий (незавершённый логин)"
mkdir -p data/sessions
rm -f data/sessions/max_acc_*.db data/sessions/max_acc_*.db-* || true

echo "==> docker compose up -d --build"
docker compose up -d --build

echo "==> проверка UI (ожидается api.js?v=31 и max-login-v31)"
sleep 2
curl -fsS http://127.0.0.1:3005/ | grep -oE 'api\.js\?v=[0-9]+' | head -1 || true
curl -fsS http://127.0.0.1:3005/ | grep -o 'max-login-v31' | head -1 || true

echo "Готово. Снаружи:"
echo "  curl -s https://admin.veresk-flowers.ru/ | grep -oE 'api.js\\?v=[0-9]+|max-login-v31'"
echo "В браузере: Ctrl+Shift+R, затем снова подключите номер MAX."
