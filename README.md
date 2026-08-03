# Veresk — Telegram-бот цветочного магазина

Бот для приёма заявок на букеты: пошаговая анкета для клиента, уведомление флориста в Telegram и создание заказа в [Posiflora](https://posiflora.com).

## Возможности

- Диалог заказа: 7 шагов для нового клиента; для повторного — сразу с даты (имя и телефон из базы)
- SQLite: профиль клиента и история заказов (`/orders` в боте, блок на главной Mini App)
- Кнопка «Поделиться номером» и ручной ввод телефона
- Создание заказа в Posiflora API (JSON:API)
- Уведомление флориста с кнопками: принять / позвонить / написать / отклонить
- Уведомления клиенту при принятии или отклонении заказа
- **Telegram Mini App** — 4 экрана: главная, заказ, статус, подтверждение (брендбук Veresk)
- Трекер заказа в реальном времени через API
- **MAX-бот** (`max_bot/`) — та же анкета в мессенджере MAX, без Mini App
- **Чаты MAX в админке** — вкладка «Чаты» → MAX: история из Max API, realtime через webhook → SSE

## MAX-бот

Аналог Telegram-бота в мессенджере MAX (тот же сценарий анкеты, Posiflora, SQLite), без Mini App.

1. Создайте бота у `@MasterBot` в MAX и получите токен.
2. В `.env` заполните `MAX_BOT_TOKEN=` (и опционально `MAX_FLORIST_CHAT_ID=` — чат флориста в MAX для уведомлений об анкетах).
3. Запуск:
   - Docker: `docker compose up -d max_bot` (сервис уже описан в `docker-compose.yml`);
   - локально: из папки `veresk_bot` — `python -m max_bot.main`.

**Realtime / webhook (рекомендуется для продакшена):** задайте в `.env`

```
MAX_WEBHOOK_URL=https://florist.skypath.fun/api/max/webhook
MAX_WEBHOOK_SECRET=длинный_секрет_5_256
```

При активной подписке Max перестаёт отдавать long polling: анкета и инбокс обрабатывает сервис `bot` (`POST /api/max/webhook` → SSE в админку). Список диалогов хранится локально (`max_dialogs`), история сообщений — `GET /messages`.

Особенности MAX Bot API: клавиатуры только inline (выбор — кнопки под сообщением), телефон запрашивается кнопкой `request_contact`. Без webhook обновления читаются через long polling `GET /updates`, а SSE-хаб админки получает события через `MAX_HUB_NOTIFY_URL`. Анкеты — таблица `max_profiles`; рассылки MAX доставляются клиентам, прошедшим анкету.

## Структура проекта

```
veresk_bot/
├── miniapp/            # Telegram Mini App (HTML/CSS/JS)
│   ├── index.html
│   ├── css/style.css
│   └── js/ (app.js, order.js, status.js)
├── bot.py              # бот + приём web_app_data
├── config.py
├── webapp_server.py    # API /api/order-status, /api/order/active
├── nginx.conf          # HTTPS + раздача miniapp/
├── client_db.py        # SQLite: клиенты и история заказов
├── order_service.py    # создание заказа (Posiflora + Redis + БД)
├── notifications.py
├── posiflora.py
├── docker-compose.yml  # bot + nginx + redis
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
| `POSIFLORA_USERNAME` | Логин Posiflora |
| `POSIFLORA_PASSWORD` | Пароль Posiflora |
| `POSIFLORA_STORE_ID` | ID магазина в Posiflora |
| `POSIFLORA_BASE_URL` | URL API (по умолчанию demo) |
| `REDIS_URL` | Redis для FSM, polling и Mini App |
| `MINIAPP_URL` | Публичный **HTTPS**-адрес Mini App (`https://florist.skypath.fun/miniapp/`) |
| `WEBAPP_PORT` | Порт API внутри контейнера `bot` (по умолчанию `3005`) |
| `DATABASE_PATH` | Путь к SQLite (в Docker: `/app/data/veresk.db`, том `./data`) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Логин и пароль админки (`https://florist.skypath.fun/admin/`) |

**Как узнать `FLORIST_CHAT_ID`:** напишите боту [@userinfobot](https://t.me/userinfobot) из нужного чата или добавьте бота в группу и посмотрите `chat.id` в логах.

### 2. Docker (рекомендуется)

```bash
cd veresk_bot
docker compose up -d --build
docker compose logs -f bot
```

### 3. Локальный запуск

Требуется **Python 3.10+** (в Docker используется 3.11).

```bash
cd veresk_bot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

## Mini App

Домен проекта: **florist.skypath.fun**.

1. Для HTTPS используйте `nginx.ssl.conf.example` (уже с `server_name florist.skypath.fun`) или проксируйте 443 → контейнер.
2. Положите сертификаты Let's Encrypt в `veresk_bot/ssl/` (`fullchain.pem`, `privkey.pem`), если SSL на этом nginx.
3. В `.env`: `MINIAPP_URL=https://florist.skypath.fun/miniapp/` — **тот же URL**, что открывает Telegram (со слэшем в конце).
4. `docker compose up -d --build`
5. В [@BotFather](https://t.me/BotFather) → ваш бот → **Bot Settings** → **Domain** — `florist.skypath.fun`.
6. Mini App открывается **только inline-кнопками** в чате (`/start`, после заказа, `/orders`).
7. Админка рассылок: `https://florist.skypath.fun/admin/` (логин/пароль — `ADMIN_USERNAME` / `ADMIN_PASSWORD` в `.env`).

Команды бота: `/start` — inline «Статус заказа», `/order` — заказ в чате.

Заказ из Mini App уходит в бот через `tg.sendData()` или запасной `POST /api/order/submit` (с подписью `initData`). Статус на главной — `/api/order/active` (polling каждые 15 с).

### Если Mini App «как обычный сайт»

| Симптом | Причина |
|--------|---------|
| Нет профиля и истории заказов | Открыли URL в Safari/Chrome, а не в Telegram — `initData` пустой |
| Заказ не доходит до бота | То же: вне Telegram нет `sendData` и авторизации API |
| Всё пусто после деплоя | `MINIAPP_URL` не совпадает с реальным адресом или домен не добавлен в BotFather |
| API 401 | Другой `BOT_TOKEN` на сервере, не тот бот, из которого открыли приложение |

**Правильно:** Telegram → ваш бот → `/start` → inline «📋 Статус заказа» или «Следить за заказом» после заказа.  
**Неправильно:** вставить ссылку `https://.../miniapp/` в браузер.

Для Mini App нужен **HTTPS** (не `http://`), кроме локальной отладки.

## Деплой на сервер

```bash
git clone <url-репозитория>
cd veresk_bot
cp .env.example .env && nano .env
docker compose up -d --build
```

## Лицензия

Проприетарный проект Veresk. Все права защищены.
