# Деплой: admin.veresk-flowers.ru

Продакшен-домен: **https://admin.veresk-flowers.ru**

| URL | Назначение |
|-----|------------|
| `/admin/` | Админ-панель (рассылки, аккаунты, чаты MAX) |
| `/miniapp/` | Telegram Mini App |
| `/api/` | API бота и админки |
| `/api/max/webhook` | Webhook MAX-бота (realtime) |

Сервисы Docker: `bot` (Telegram + API), `max_bot`, `nginx`, `redis`.

---

## 1. DNS

A-запись:

```
admin.veresk-flowers.ru  →  IP вашего сервера
```

---

## 2. Код на сервере

Репозиторий на GitHub содержит вложенную папку `veresk_bot/`. После клона путь часто такой:

```text
~/veresk/veresk_bot/           ← корень git, иногда старый compose + .env
~/veresk/veresk_bot/veresk_bot/ ← актуальный код, deploy.sh, docker-compose.yml
```

**Деплой всегда из внутренней папки** (где лежат `deploy.sh` и свежий `adminapp/`):

```bash
cd ~/veresk/veresk_bot/veresk_bot
# один раз: остановить старый стек снаружи, если он слушает :3005
cd .. && docker compose down 2>/dev/null; cd veresk_bot

chmod +x deploy.sh && ./deploy.sh
# скрипт сам подхватит ../.env, ../data, ../logs при необходимости
```

**Проверка, что выкатилось (иначе UI останется на старом `api.js?v=26`):**

```bash
curl -s https://admin.veresk-flowers.ru/ | grep -oE 'api.js\?v=[0-9]+|max-login-v31'
# ожидается: api.js?v=31  и  max-login-v31
```

Если видите `v=26` — Docker/nginx запущены из **внешней** папки со старым `adminapp`, а не из `.../veresk_bot/veresk_bot`.

Сохраните `./data/` (SQLite, сессии) и `.env`.

---

## 3. `.env` на сервере

Добавьте / обновите (секреты не коммитьте):

```bash
PUBLIC_BASE_URL=https://admin.veresk-flowers.ru
MINIAPP_URL=https://admin.veresk-flowers.ru/miniapp/
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<сложный_пароль>
MAX_WEBHOOK_URL=https://admin.veresk-flowers.ru/api/max/webhook
MAX_WEBHOOK_SECRET=<случайная_строка_16+_символов>
MAX_HUB_NOTIFY_URL=http://bot:3005/api/internal/max/event
REDIS_URL=redis://redis:6379
DATABASE_PATH=/app/data/veresk.db
```

`BOT_TOKEN`, Posiflora и остальное — как сейчас. Токен MAX можно задать в `.env` (`MAX_BOT_TOKEN=`) или позже в админке.

---

## 4. HTTPS (рекомендуется: nginx на хосте)

Docker слушает только **3005**. SSL и домен — на хосте (не конфликтует с другими сайтами).

```bash
# 1) Запуск стека
docker compose up -d --build

# 2) Конфиг хоста
sudo cp nginx.host-proxy.conf.example /etc/nginx/sites-available/admin.veresk-flowers.ru
sudo ln -sf /etc/nginx/sites-available/admin.veresk-flowers.ru /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 3) Сертификат
sudo certbot --nginx -d admin.veresk-flowers.ru
```

Проверка: `curl -I https://admin.veresk-flowers.ru/admin/`

### Альтернатива: SSL внутри Docker

```bash
cp nginx.ssl.conf.example nginx.conf
# положите fullchain.pem + privkey.pem в ./ssl/
# в docker-compose.yml раскомментируйте порты 80/443 и volume ./ssl
docker compose up -d --build
```

---

## 5. Telegram

1. [@BotFather](https://t.me/BotFather) → бот → **Bot Settings** → **Domain** → `admin.veresk-flowers.ru`
2. Mini App открывается только из чата бота (кнопки Web App), не из браузера.

---

## 6. MAX-бот

1. Токен от `@MasterBot` → админка → **Аккаунты** → MAX (или `MAX_BOT_TOKEN` в `.env`)
2. Webhook: `https://admin.veresk-flowers.ru/api/max/webhook` + секрет
3. После сохранения webhook long polling у `max_bot` отключается — события идут в сервис `bot`

```bash
docker compose logs -f max_bot bot
```

---

## 7. Проверка после деплоя

```bash
docker compose ps
docker compose logs --tail=80 bot
curl -fsS https://admin.veresk-flowers.ru/admin/ | head
curl -fsS -o /dev/null -w "%{http_code}\n" https://admin.veresk-flowers.ru/miniapp/
```

- Админка: логин/пароль из `.env`
- Telegram: `/start` → Mini App / анкета
- MAX: диалог с ботом → анкета; в админке вкладка «Чаты» → MAX

---

## Обновление без простоя данных

```bash
cd /path/to/veresk_bot
git pull
docker compose up -d --build
```

Том `./data` и volume `redis_data` сохраняются. Не удаляйте `./data/veresk.db` и `./data/sessions/`.
