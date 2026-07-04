# botsecurity — Telegram-бот реестра образов ИБ

Публичный Telegram-бот для мониторинга [Google-таблицы](https://docs.google.com/spreadsheets/d/1nRDstaHXnZ2Jf9IvgsAAvM8anro9JpjB/edit) с образами для проверки ИБ.

## Возможности

- Опрос таблицы каждый час (настраивается)
- Уведомления подписчикам о новых образах и изменениях
- Напоминания в рабочие дни (10:00, 13:00, 16:00, 18:00 МСК) о непереданных образах
- Публичные команды для всех пользователей
- Админ-команды для управления

## Команды

| Команда | Описание |
|---------|----------|
| `/start` | Подписка на уведомления |
| `/help` | Справка |
| `/pending` | Образы без статуса / не переданы |
| `/status` | Сводка по статусам |
| `/today` | Добавленные сегодня |
| `/by_dev Зуев` | Образы разработчика |
| `/stale 3` | Висят без статуса ≥ N дней |
| `/subscribe` / `/unsubscribe` | Подписка / отписка |
| `/sync` | *(админ)* Принудительная синхронизация |
| `/stats` | *(админ)* Статистика бота |

## Быстрый старт (Docker на VDS)

```bash
git clone https://github.com/RsNest/botsecurity.git
cd botsecurity

# Создать .env из примера и заполнить
cp .env.example .env
nano .env

# Google Service Account (рекомендуется)
cp credentials.json.example credentials.json
# Вставить реальный JSON, расшарить таблицу на client_email

docker compose up -d --build
docker compose logs -f
```

## Переменные окружения (.env)

```env
TELEGRAM_TOKEN=...           # от @BotFather
ADMIN_IDS=145212489          # ваш Telegram user id
SPREADSHEET_ID=1nRDstaHXnZ2Jf9IvgsAAvM8anro9JpjB
SHEET_GID=684739217
GOOGLE_CREDENTIALS_PATH=credentials.json
POLL_INTERVAL_MINUTES=60
REMINDER_HOURS=10,13,16,18
TIMEZONE=Europe/Moscow
```

## Доступ к Google Sheets

**Вариант 1 (рекомендуется):** Service Account

1. [Google Cloud Console](https://console.cloud.google.com/) → новый проект
2. APIs & Services → Enable **Google Sheets API**
3. Credentials → Create Service Account → Create Key (JSON)
4. Сохранить как `credentials.json`
5. Расшарить таблицу на email вида `xxx@project.iam.gserviceaccount.com` (Reader)

**Вариант 2:** CSV export — если таблица доступна «Anyone with the link», бот попробует скачать без credentials.

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполнить
python -m bot.main
```

## Структура

```
bot/
  main.py        — точка входа
  handlers.py    — команды + планировщик
  monitor.py     — сравнение с предыдущим состоянием
  sheets.py      — чтение Google Sheets
  storage.py     — SQLite (подписчики, снапшоты)
  formatters.py  — сообщения в Telegram
data/            — SQLite база (volume)
```

## Безопасность

- **Не коммитьте** `.env` и `credentials.json`
- Токен бота и ключи храните только на сервере
- При утечке токена — перевыпустите через @BotFather (`/revoke`)
