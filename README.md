# nikonVPN Telegram Bot

Telegram бот для продажи VPN доступа с интеграцией WGDashboard и поддержкой платежей через Telegram Stars и ЮKassa.

## 🚀 Возможности

- **Многотарифная система**: 14 дней, 30 дней, 90 дней
- **Два способа оплаты**: Telegram Stars и ЮKassa (банковские карты)
- **Автоматическое создание VPN**: Интеграция с WGDashboard
- **Уведомления**: Автоматические напоминания об истечении доступа
- **Управление доступом**: Продление, проверка статуса, получение конфигурации
- **Безопасность**: Все секреты в переменных окружения

## 📋 Тарифы

| Период | Telegram Stars | ЮKassa |
|--------|----------------|--------|
| 14 дней | 100 Stars | 150 ₽ |
| 30 дней | 200 Stars | 300 ₽ |
| 90 дней | 500 Stars | 750 ₽ *(временно недоступен)* |

## 🛠 Установка

### 1. Клонирование репозитория

```bash
git clone <repository-url>
cd wgbot
```

### 2. Создание виртуального окружения

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# или
venv\Scripts\activate     # Windows
```

### 3. Установка зависимостей

```bash
pip install -r requirements.txt
```

### 4. Настройка переменных окружения

Скопируйте файл `config/env_example.txt` в `.env` и заполните:

```bash
cp config/env_example.txt .env
```

Отредактируйте `.env`:

```env
# Telegram Bot Token (получить у @BotFather)
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

# WGDashboard Configuration
WG_DASHBOARD_URL=http://your-wg-dashboard-url:10086
WG_DASHBOARD_API_KEY=your_wg_dashboard_api_key_here
WG_CONFIG_NAME=awg0

# YooKassa Configuration
YOOKASSA_SHOP_ID=your_yookassa_shop_id_here
YOOKASSA_SECRET_KEY=your_yookassa_secret_key_here
YOOKASSA_PROVIDER_TOKEN=your_yookassa_provider_token_here
```

### 5. Создание необходимых директорий

```bash
mkdir -p data logs
```

## 🔧 Настройка

### Telegram Bot

1. Создайте бота через [@BotFather](https://t.me/BotFather)
2. Получите токен бота
3. Настройте команды бота:
   ```
   start - Главное меню
   buy - Купить доступ
   connect - Получить конфигурацию
   extend - Продлить доступ
   status - Проверить статус
   ```

### WGDashboard

1. Установите и настройте WGDashboard
2. Получите API ключ
3. Убедитесь, что API доступен по указанному URL

### ЮKassa (опционально)

1. Зарегистрируйтесь в [ЮKassa](https://yookassa.ru/)
2. Получите Shop ID и Secret Key
3. Настройте провайдера в BotFather:
   - Отправьте `/mybots` в BotFather
   - Выберите вашего бота
   - Нажмите "Bot Settings" → "Payments"
   - Добавьте провайдера ЮKassa
   - Получите Provider Token

## 🚀 Запуск

### Разработка

```bash
python bot.py
```

### Продакшен

```bash
python run.py
```

## 📁 Структура проекта

```
wgbot/
├── bot.py                 # Основной файл бота
├── config.py             # Конфигурация
├── database.py           # Работа с базой данных
├── payment.py            # Обработка платежей
├── utils.py              # Утилиты
├── wg_api.py             # API WGDashboard
├── run.py                # Точка входа для продакшена
├── requirements.txt      # Зависимости Python
├── .gitignore           # Игнорируемые файлы
├── README.md            # Документация
├── config/
│   └── env_example.txt  # Пример переменных окружения
├── data/
│   └── wgbot.db         # База данных SQLite
└── logs/
    └── wgbot.log        # Логи приложения
```

## 🔐 Безопасность

- Все секретные данные хранятся в переменных окружения
- Файл `.env` добавлен в `.gitignore`
- База данных и логи не попадают в репозиторий
- API ключи не логируются

## 📊 База данных

Бот использует SQLite базу данных со следующей структурой:

### Таблица `peers`
- `id` - Уникальный идентификатор
- `peer_name` - Имя пира (username_telegramID)
- `peer_id` - ID пира в WGDashboard
- `job_id` - ID job для ограничения времени
- `telegram_user_id` - ID пользователя Telegram
- `telegram_username` - Username пользователя
- `created_at` - Дата создания
- `expire_date` - Дата истечения доступа
- `is_active` - Активен ли пир
- `payment_status` - Статус оплаты
- `stars_paid` - Количество оплаченных звезд
- `last_payment_date` - Дата последней оплаты
- `notification_sent` - Отправлено ли уведомление
- `tariff_key` - Ключ тарифа
- `payment_method` - Способ оплаты
- `rub_paid` - Количество оплаченных рублей

## 🔄 API WGDashboard

Бот интегрируется с WGDashboard через REST API:

- `POST /api/peers` - Создание пира
- `GET /api/peers/{peer_id}/config` - Получение конфигурации
- `DELETE /api/peers/{peer_id}` - Удаление пира
- `POST /api/savePeerScheduleJob` - Создание job для ограничения времени
- `DELETE /api/deletePeerScheduleJob` - Удаление job

## 💳 Платежи

### Telegram Stars
- Автоматическая обработка через Telegram API
- Поддержка всех тарифов
- Мгновенное подтверждение

### ЮKassa
- Поддержка банковских карт
- Автоматическое подтверждение платежей
- Логирование всех транзакций

## 🔔 Уведомления

Бот автоматически отправляет уведомления:
- За 1 день до истечения доступа
- При истечении доступа
- При успешной оплате

## 📝 Логирование

Все события логируются в файл `logs/wgbot.log`:
- Запуск и остановка бота
- Создание и удаление пиров
- Обработка платежей
- Ошибки и исключения

## 🐛 Отладка

### Проверка логов
```bash
tail -f logs/wgbot.log
```

### Проверка базы данных
```bash
sqlite3 data/wgbot.db
.tables
SELECT * FROM peers;
```

### Проверка конфигурации
```bash
python -c "from config import *; print('Config loaded successfully')"
```

## 🚀 Деплой

### Systemd (Linux)

Создайте файл `/etc/systemd/system/wgbot.service`:

```ini
[Unit]
Description=nikonVPN Telegram Bot
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/wgbot
Environment=PATH=/path/to/wgbot/venv/bin
ExecStart=/path/to/wgbot/venv/bin/python run.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Запустите сервис:
```bash
sudo systemctl enable wgbot
sudo systemctl start wgbot
sudo systemctl status wgbot
```

### Docker (опционально)

Создайте `Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN mkdir -p data logs

CMD ["python", "run.py"]
```

## 📞 Поддержка

При возникновении проблем:
1. Проверьте логи в `logs/wgbot.log`
2. Убедитесь, что все переменные окружения настроены
3. Проверьте доступность WGDashboard API
4. Убедитесь, что бот имеет необходимые права

## 📄 Лицензия

Проект распространяется под лицензией MIT.

## 🔄 Обновления

Для обновления бота:
1. Остановите бота
2. Сделайте бэкап базы данных
3. Обновите код
4. Запустите бота

```bash
# Бэкап базы данных
cp data/wgbot.db data/wgbot.db.backup

# Обновление
git pull
pip install -r requirements.txt

# Запуск
python run.py
```

---

**nikonVPN Telegram Bot** - надежное решение для продажи VPN доступа через Telegram! 🚀