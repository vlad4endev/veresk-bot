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
| `MINIAPP_URL` | Публичный **HTTPS**-адрес Mini App (например `https://orders.veresk.ru/miniapp/`) |
| `WEBAPP_PORT` | Порт API внутри контейнера `bot` (по умолчанию `8080`) |
| `DATABASE_PATH` | Путь к SQLite (в Docker: `/app/data/veresk.db`, том `./data`) |

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

1. В `nginx.conf` замените `YOUR_DOMAIN` на ваш домен.
2. Положите сертификаты Let's Encrypt в `veresk_bot/ssl/` (`fullchain.pem`, `privkey.pem`).
3. В `.env`: `MINIAPP_URL=https://ваш-домен/miniapp/`
4. `docker compose up -d --build`
5. В [@BotFather](https://t.me/BotFather) → **Menu Button** → URL Mini App.

Команды бота: `/start` — открыть приложение, `/order` — заказ в чате (как раньше).

Заказ из Mini App уходит в бот через `tg.sendData()` → создаётся заказ в Posiflora. Статус на главной обновляется через `/api/order/active` (polling каждые 15 с).

## Деплой на сервер

```bash
git clone <url-репозитория>
cd veresk_bot
cp .env.example .env && nano .env
docker compose up -d --build
```

## Лицензия

Проприетарный проект Veresk. Все права защищены.
