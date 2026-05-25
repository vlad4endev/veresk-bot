# Veresk — Telegram-бот цветочного магазина

Бот для приёма заявок на букеты: пошаговая анкета для клиента, уведомление флориста в Telegram и создание заказа в [Posiflora](https://posiflora.com).

## Возможности

- Диалог заказа из 7 шагов (имя, телефон, дата, получатель, повод, связь, бюджет)
- Кнопка «Поделиться номером» и ручной ввод телефона
- Создание заказа в Posiflora API (JSON:API)
- Уведомление флориста с кнопками: принять / позвонить / написать / отклонить
- Уведомления клиенту при принятии или отклонении заказа

## Структура проекта

```
veresk_bot/
├── bot.py              # FSM-диалог и точка входа
├── config.py           # переменные окружения
├── notifications.py    # уведомления флористу и callback-кнопки
├── posiflora.py        # интеграция с Posiflora API
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
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

## Деплой на сервер

```bash
git clone <url-репозитория>
cd veresk_bot
cp .env.example .env && nano .env
docker compose up -d --build
```

## Лицензия

Проприетарный проект Veresk. Все права защищены.
