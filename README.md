# nikonVPN Telegram Bot

Telegram бот для продажи VPN доступа с интеграцией WGDashboard и поддержкой платежей через Telegram Stars и ЮKassa.

## 🚀 Возможности

- **Многотарифная система**: 14 дней, 30 дней
- **Два способа оплаты**: Telegram Stars и ЮKassa
- **Автоматическое создание VPN**: Интеграция с WGDashboard
- **Уведомления**: Автоматические напоминания об истечении доступа

## 📋 Тарифы

| Период | Telegram Stars | ЮKassa |
|--------|----------------|--------|
| 14 дней | 100 Stars | 150 ₽ |
| 30 дней | 200 Stars | 300 ₽ |

## 🛠 Установка

### 1. Клонирование репозитория

```bash
git clone https://github.com/alexnikon/wgbot.git
cd wgbot
```

### 2. Настройка конфигурации

```bash
cp env.docker.example .env
nano .env  # Заполните реальными данными
```

### 3. Запуск

```bash
docker-compose up -d
```

> **Примечание**: Используется готовый образ `goomboldt/wgbot:latest` из DockerHub

## ⚙️ Конфигурация (.env)

```env
# Telegram Bot Token (получить у @BotFather)
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

# WGDashboard Configuration
WG_DASHBOARD_URL=http://your-wg-dashboard-url:10086
WG_DASHBOARD_API_KEY=your_wg_dashboard_api_key_here

# YooKassa Configuration
YOOKASSA_SHOP_ID=your_yookassa_shop_id_here
YOOKASSA_SECRET_KEY=your_yookassa_secret_key_here
YOOKASSA_PROVIDER_TOKEN=your_yookassa_provider_token_here

# Logging
LOG_LEVEL=INFO
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

### ЮKassa

1. Зарегистрируйтесь в [ЮKassa](https://yookassa.ru/)
2. Получите Shop ID и Secret Key
3. Настройте провайдера в BotFather:
   - Отправьте `/mybots` в BotFather
   - Выберите вашего бота
   - Нажмите "Bot Settings" → "Payments"
   - Добавьте провайдера ЮKassa
   - Получите Provider Token

## 🚀 Управление

```bash
# Запуск
docker-compose up -d

# Остановка
docker-compose down

# Просмотр логов
docker-compose logs -f

# Перезапуск
docker-compose restart

# Обновление образа
docker-compose pull
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
├── run.py                # Точка входа
├── requirements.txt      # Зависимости Python
├── Dockerfile            # Docker образ
├── docker-compose.yml    # Docker Compose конфигурация
├── env.docker.example    # Пример конфигурации
└── wgdashboard_doc.txt   # Документация WGDashboard API
```

## 🔐 Безопасность

- Все секретные данные хранятся в переменных окружения
- Файл `.env` добавлен в `.gitignore`
- База данных и логи не попадают в репозиторий
- Контейнер запускается под непривилегированным пользователем
- Read-only файловая система
- Ограничения ресурсов (512MB RAM, 0.5 CPU)

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
docker-compose logs -f
```

### Проверка базы данных
```bash
docker exec -it wgbot sqlite3 /app/data/wgbot.db
.tables
SELECT * FROM peers;
```

---

**nikonVPN Telegram Bot** - надежное решение для продажи VPN доступа через Telegram! 🚀