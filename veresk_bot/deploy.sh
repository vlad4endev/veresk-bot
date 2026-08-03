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
# nginx кеширует IP upstream при старте — после recreate bot нужен reload
docker compose up -d nginx
docker compose exec -T nginx nginx -s reload 2>/dev/null || docker compose restart nginx

echo "==> ждём API (/api/health), до 90с"
ready=0
for i in $(seq 1 90); do
  if curl -fsS --max-time 2 http://127.0.0.1:3005/api/health >/dev/null 2>&1; then
    echo "health_ok after ${i}s"
    ready=1
    break
  fi
  # прямой health внутри bot — отделяет «бот мёртв» от «nginx DNS»
  if docker compose exec -T bot python -c "
import urllib.request
urllib.request.urlopen('http://127.0.0.1:3005/api/health', timeout=2)
print('bot_direct_health=ok')
" 2>/dev/null; then
    echo "bot API жив, но nginx ещё не проксирует — reload nginx"
    docker compose exec -T nginx nginx -s reload 2>/dev/null || docker compose restart nginx
  fi
  if (( i % 5 == 0 )); then
    echo "  … ещё ждём (${i}s), bot=$(docker compose ps --status running --format '{{.Status}}' bot 2>/dev/null | head -1 || echo '?')"
  fi
  sleep 1
done

echo "==> статус контейнеров"
docker compose ps

if [[ "$ready" -ne 1 ]]; then
  echo "WARN: /api/health не ответил за 90с — смотрите логи ниже"
fi

echo "==> проверка UI/API"
curl -fsS http://127.0.0.1:3005/ | grep -oE 'api\.js\?v=[0-9]+' | head -1 || true
curl -fsS http://127.0.0.1:3005/ | grep -oE 'app\.js\?v=[0-9]+' | head -1 || true
docker compose exec -T bot python -c "
import urllib.request
req=urllib.request.Request('http://127.0.0.1:3005/api/admin/login', data=b'{\"username\":\"x\",\"password\":\"y\"}', headers={'Content-Type':'application/json'}, method='POST')
try:
    urllib.request.urlopen(req, timeout=5)
except Exception as e:
    print('bot_direct_login=', getattr(e, 'code', e))
" 2>/dev/null || true
login_code="$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST http://127.0.0.1:3005/api/admin/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"x","password":"y"}' || echo fail)"
echo "login_via_nginx=${login_code}"
if [[ "$login_code" == "502" || "$login_code" == "000" || "$login_code" == "fail" ]]; then
  echo "==> login ещё 502 — reload nginx и повтор через 3с"
  docker compose exec -T nginx nginx -s reload 2>/dev/null || docker compose restart nginx
  sleep 3
  curl -sS -o /dev/null -w "login_via_nginx_retry=%{http_code}\n" \
    -X POST http://127.0.0.1:3005/api/admin/login \
    -H "Content-Type: application/json" \
    -d '{"username":"x","password":"y"}' || true
fi
docker compose logs --tail=60 bot || true
docker compose logs --tail=20 nginx || true

echo "Готово. Снаружи:"
echo "  curl -s https://admin.veresk-flowers.ru/ | grep -oE 'api.js\\?v=[0-9]+|app.js\\?v=[0-9]+'"
echo "  curl -s -o /dev/null -w '%{http_code}\\n' https://admin.veresk-flowers.ru/api/health"
echo "  curl -s -o /dev/null -w '%{http_code}\\n' -X POST https://admin.veresk-flowers.ru/api/admin/login -H 'Content-Type: application/json' -d '{\"username\":\"x\",\"password\":\"y\"}'"
echo "В браузере: Ctrl+Shift+R"
