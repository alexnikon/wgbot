# nikonVPN Telegram Bot

Telegram бот для продажи VPN доступа с интеграцией WGDashboard и поддержкой платежей через Telegram Stars и ЮKassa.

## 🌿 Ветки

- **`main`** - Стабильная версия для продакшена
- **`dev`** - Ветка разработки с Docker поддержкой

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

### Вариант 1: Docker (Рекомендуется)

#### 1. Клонирование репозитория

```bash
git clone https://github.com/alexnikon/wgbot.git
cd wgbot
git checkout dev  # Переключаемся на ветку с Docker
```

#### 2. Настройка конфигурации

```bash
# Копируем пример конфигурации
cp env.docker.example .env

# Редактируем конфигурацию
nano .env
```

#### 3. Запуск бота

```bash
# Используем удобный скрипт управления
./docker-manage.sh start

# Просмотр логов
./docker-manage.sh logs

# Остановка
./docker-manage.sh stop
```

#### Управление Docker контейнером

```bash
# Все доступные команды
./docker-manage.sh help

# Запуск бота
./docker-manage.sh start

# Остановка бота
./docker-manage.sh stop

# Перезапуск бота
./docker-manage.sh restart

# Просмотр логов
./docker-manage.sh logs

# Просмотр последних 100 строк логов
./docker-manage.sh logs-tail

# Статус бота
./docker-manage.sh status

# Обновление бота
./docker-manage.sh update

# Сборка образа
./docker-manage.sh build
```

#### Прямое использование Docker Compose

```bash
# Запуск
docker-compose up -d

# Остановка
docker-compose down

# Просмотр логов
docker-compose logs -f wgbot

# Перезапуск
docker-compose restart

# Сборка
docker-compose build
```

#### Мониторинг логов (опционально)

Для удобного просмотра логов можно запустить Dozzle:

```bash
# Запуск с мониторингом
docker-compose --profile monitoring up -d

# Доступ к веб-интерфейсу
# http://localhost:9999
```



### Вариант 2: Локальная установка

#### 1. Клонирование репозитория

```bash
git clone https://github.com/alexnikon/wgbot.git
cd wgbot
```

#### 2. Создание виртуального окружения

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# или
venv\Scripts\activate     # Windows
```

#### 3. Установка зависимостей

```bash
pip install -r requirements.txt
```

#### 4. Настройка переменных окружения

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

#### 5. Создание необходимых директорий

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

### Docker (Рекомендуется)

```bash
# Запуск
./docker-manage.sh start

# Просмотр логов
./docker-manage.sh logs

# Остановка
./docker-manage.sh stop
```

### Локальная установка

#### Разработка

```bash
python bot.py
```

#### Продакшен

```bash
python run.py
```

## 📁 Структура проекта

```
wgbot/
├── bot.py                    # Основной файл бота
├── config.py                # Конфигурация
├── database.py              # Работа с базой данных
├── payment.py               # Обработка платежей
├── utils.py                 # Утилиты
├── wg_api.py                # API WGDashboard
├── run.py                   # Точка входа для продакшена
├── requirements.txt         # Зависимости Python
├── .gitignore              # Игнорируемые файлы
├── README.md               # Документация
├── Dockerfile              # Docker образ
├── docker-compose.yml      # Docker Compose конфигурация (dev)
├── docker-compose.prod.yml # Docker Compose конфигурация (prod)
├── .dockerignore           # Игнорируемые файлы для Docker
├── docker-manage.sh        # Скрипт управления Docker (dev)
├── deploy-prod.sh          # Скрипт продакшен деплоя
├── check_config.py         # Скрипт проверки конфигурации
├── env.docker.example      # Пример конфигурации для Docker
├── wgdashboard_doc.txt     # Документация WGDashboard API
├── config/
│   └── env_example.txt     # Пример переменных окружения
├── data/                   # База данных (создается автоматически)
└── logs/                   # Логи (создаются автоматически)
```

## 🔐 Безопасность

### Передача секретов
- **Переменные окружения** - Все секреты передаются через `.env` файл
- **Docker secrets** - Секреты не хранятся в образах
- **Валидация** - Автоматическая проверка конфигурации перед запуском

### Хранение секретов
- **Локально:** `.env` файл (добавлен в `.gitignore`)
- **Продакшен:** Переменные окружения сервера или Docker secrets
- **CI/CD:** Переменные окружения в системе сборки

### Рекомендации для продакшена
```bash
# 1. Создайте .env файл на сервере
cp env.docker.example .env
nano .env  # Заполните реальными данными

# 2. Проверьте конфигурацию
python3 check_config.py

# 3. Запустите продакшен версию
./deploy-prod.sh deploy
```

### Безопасность контейнера
- **Read-only файловая система** - Защита от изменений
- **No-new-privileges** - Ограничение привилегий
- **Resource limits** - Ограничения памяти и CPU
- **Health checks** - Автоматический мониторинг
- **Non-root user** - Запуск под непривилегированным пользователем

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

### Docker (Рекомендуется)

```bash
# Клонирование и настройка
git clone https://github.com/alexnikon/wgbot.git
cd wgbot
git checkout dev
cp env.docker.example .env
nano .env  # Настройте конфигурацию

# Запуск
./docker-manage.sh start

# Мониторинг
./docker-manage.sh logs
```

#### Особенности Docker развертывания

- **Alpine Linux** - Минимальный размер образа (~400MB)
- **Health checks** - Автоматическая проверка состояния
- **Автоматический перезапуск** - При сбоях
- **Монтирование данных** - Персистентная база данных и логи
- **Безопасность** - Запуск под непривилегированным пользователем
- **Мониторинг** - Опциональный Dozzle сервис для логов

#### Требования для Docker

- Docker Engine 20.10+
- Docker Compose 2.0+
- 512MB RAM
- 1GB свободного места

#### Продакшен развертывание

```bash
# Используйте продакшен конфигурацию
./deploy-prod.sh deploy

# Обновление
./deploy-prod.sh update

# Проверка статуса
./deploy-prod.sh status
```

**Особенности продакшен версии:**
- Ограничения ресурсов (512MB RAM, 0.5 CPU)
- Read-only файловая система
- Безопасные настройки (no-new-privileges)
- Named volumes для данных
- Автоматические health checks



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

---
**nikonVPN Telegram Bot** - надежное решение для продажи VPN доступа через Telegram! 🚀