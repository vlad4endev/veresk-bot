# Veresk — Telegram / MAX бот цветочного магазина

Бот для приёма заявок на букеты: пошаговая анкета для клиента, уведомление флориста и создание заказа в [Posiflora](https://posiflora.com).

**Продакшен-домен:** [admin.veresk-flowers.ru](https://admin.veresk-flowers.ru)  
Подробный деплой: [`veresk_bot/DEPLOY.md`](veresk_bot/DEPLOY.md)

## Возможности

- Диалог заказа: 7 шагов для нового клиента; для повторного — сразу с даты (имя и телефон из базы)
- SQLite: профиль клиента и история заказов (`/orders` в боте, блок на главной Mini App)
- Кнопка «Поделиться номером» и ручной ввод телефона
- Создание заказа в Posiflora API (JSON:API)
- Уведомление флориста с кнопками: принять / позвонить / написать / отклонить
- Уведомления клиенту при принятии или отклонении заказа
- **Telegram Mini App** — 4 экрана: главная, заказ, статус, подтверждение (брендбук Veresk)
- Трекер заказа в реальном времени через API
- **Админ-панель** — рассылки, сегменты, аккаунты, чаты MAX: `https://admin.veresk-flowers.ru/admin/`
- **MAX-бот** (`max_bot/`) — та же анкета в мессенджере MAX, без Mini App
- **Чаты MAX в админке** — вкладка «Чаты» → MAX: история из Max API, realtime через webhook → SSE

## MAX-бот

Аналог Telegram-бота в мессенджере MAX (тот же сценарий анкеты, Posiflora, SQLite), без Mini App.

1. Создайте бота у `@MasterBot` в MAX и получите токен.
2. В `.env` заполните `MAX_BOT_TOKEN=` (и опционально `MAX_FLORIST_CHAT_ID=`) или задайте в админке.
3. Запуск:
   - Docker: `docker compose up -d max_bot` (сервис уже в `docker-compose.yml`);
   - локально: из папки `veresk_bot` — `python -m max_bot.main`.

**Realtime / webhook (продакшен):**

```
MAX_WEBHOOK_URL=https://admin.veresk-flowers.ru/api/max/webhook
MAX_WEBHOOK_SECRET=длинный_секрет_5_256
```

При активной подписке Max перестаёт отдавать long polling: анкета и инбокс обрабатывает сервис `bot` (`POST /api/max/webhook` → SSE в админку).

## Структура проекта

```
veresk_bot/
├── miniapp/            # Telegram Mini App (HTML/CSS/JS)
├── adminapp/           # Админ-панель (/admin/)
├── max_bot/            # MAX-бот
├── bot.py              # Telegram-бот + API
├── admin_api.py        # API админки
├── nginx.conf          # раздача static + proxy API (:3005)
├── nginx.host-proxy.conf.example  # SSL на хосте → :3005
├── nginx.ssl.conf.example         # SSL внутри Docker
├── docker-compose.yml  # bot + max_bot + nginx + redis
├── DEPLOY.md           # пошаговый деплой
└── .env.example
```

## Быстрый старт

### 1. Переменные окружения

```bash
cd veresk_bot
cp .env.example .env
# отредактируйте .env
```

| Переменная | Описание |
|------------|----------|
| `BOT_TOKEN` | Токен бота от [@BotFather](https://t.me/BotFather) |
| `FLORIST_CHAT_ID` | Telegram ID чата/группы флориста |
| `POSIFLORA_*` | Учётные данные Posiflora |
| `PUBLIC_BASE_URL` | `https://admin.veresk-flowers.ru` |
| `MINIAPP_URL` | `https://admin.veresk-flowers.ru/miniapp/` |
| `REDIS_URL` | Redis для FSM и Mini App |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Вход в админку |
| `MAX_BOT_TOKEN` / `MAX_WEBHOOK_*` | MAX-бот и webhook |

### 2. Docker (рекомендуется)

```bash
cd veresk_bot
docker compose up -d --build
docker compose logs -f bot
```

### 3. Локальный запуск

Требуется **Python 3.10+** (в Docker — 3.11).

```bash
cd veresk_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
# админка локально: python run_admin_local.py → http://127.0.0.1:3005/admin/
```

## Mini App и админка

Домен: **admin.veresk-flowers.ru**.

1. HTTPS: хост-прокси (`nginx.host-proxy.conf.example` + certbot) или SSL в Docker (`nginx.ssl.conf.example`).
2. В `.env`: `MINIAPP_URL=https://admin.veresk-flowers.ru/miniapp/`
3. `docker compose up -d --build`
4. BotFather → **Domain** → `admin.veresk-flowers.ru`
5. Админка: `https://admin.veresk-flowers.ru/admin/`

Mini App открывается **только** из Telegram (inline Web App), не из обычного браузера.

### Если Mini App «как обычный сайт»

| Симптом | Причина |
|--------|---------|
| Нет профиля и истории | Открыли URL вне Telegram — нет `initData` |
| Заказ не доходит | Нет `sendData` вне Telegram |
| Пусто после деплоя | `MINIAPP_URL` / Domain в BotFather не совпадают |
| API 401 | Другой `BOT_TOKEN` на сервере |

## Деплой на сервер

См. **[veresk_bot/DEPLOY.md](veresk_bot/DEPLOY.md)** — DNS, `.env`, HTTPS, Telegram, MAX, проверка.

Кратко:

```bash
cd veresk_bot
# обновить PUBLIC_BASE_URL / MINIAPP_URL / ADMIN_* / MAX_WEBHOOK_* в .env
docker compose up -d --build
# на хосте: nginx.host-proxy + certbot -d admin.veresk-flowers.ru
```

## Лицензия

Проприетарный проект Veresk. Все права защищены.
