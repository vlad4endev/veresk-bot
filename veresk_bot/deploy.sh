#!/usr/bin/env bash
# Обновление admin.veresk-flowers.ru из git + пересборка Docker.
# Запускать из каталога с этим скриптом И docker-compose.yml
# (часто: ~/veresk/veresk_bot/veresk_bot после клона репо с вложенной папкой).
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f docker-compose.yml ]]; then
  echo "Ошибка: нет docker-compose.yml в $(pwd)"
  exit 1
fi

# .env часто лежит на уровень выше (клон: .../veresk_bot/.env + .../veresk_bot/veresk_bot/)
if [[ ! -f .env ]]; then
  if [[ -f ../.env ]]; then
    echo "==> .env не найден здесь — симлинк на ../.env"
    ln -sfn ../.env .env
  else
    echo "Ошибка: нет .env в $(pwd) и в $(dirname "$(pwd)")"
    echo "Скопируйте .env сюда или: ln -s /path/to/.env .env"
    exit 1
  fi
fi

# data/logs с продакшена тоже часто на уровень выше
for d in data logs certs; do
  if [[ ! -e "$d" && -d "../$d" ]]; then
    echo "==> симлинк $d -> ../$d"
    ln -sfn "../$d" "$d"
  fi
done

echo "==> git pull (корень репозитория)"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -n "$ROOT" ]]; then
  git -C "$ROOT" pull --ff-only
else
  git pull --ff-only
fi

# Не удаляем живые max_acc_*.db — иначе после деплоя пропадает личный номер MAX.
# Битые незавершённые логины чистите вручную при необходимости.

echo "==> docker compose up -d --build (cwd=$(pwd))"
docker compose up -d --build

echo "==> статус контейнеров"
docker compose ps

echo "==> проверка UI/API"
sleep 3
curl -fsS http://127.0.0.1:3005/ | grep -oE 'api\.js\?v=[0-9]+' | head -1 || true
curl -sS -o /dev/null -w "login_http=%{http_code}\n" \
  -X POST http://127.0.0.1:3005/api/admin/login \
  -H "Content-Type: application/json" \
  -d '{"username":"x","password":"y"}' || true
docker compose logs --tail=40 bot || true

echo "Готово. Снаружи:"
echo "  curl -s https://admin.veresk-flowers.ru/ | grep -oE 'api.js\\?v=[0-9]+'"
echo "  curl -s -o /dev/null -w '%{http_code}\\n' -X POST https://admin.veresk-flowers.ru/api/admin/login -H 'Content-Type: application/json' -d '{\"username\":\"x\",\"password\":\"y\"}'"
echo "В браузере: Ctrl+Shift+R"
