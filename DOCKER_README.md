# 🐳 Docker Deployment Guide

Этот документ описывает развертывание nikonVPN Telegram Bot с использованием Docker и Docker Compose.

## 📋 Требования

- Docker Engine 20.10+
- Docker Compose 2.0+
- Git

## 🚀 Быстрый старт

### 1. Клонирование репозитория

```bash
git clone https://github.com/alexnikon/wgbot.git
cd wgbot
git checkout dev
```

### 2. Настройка конфигурации

```bash
# Копируем пример конфигурации
cp env.docker.example .env

# Редактируем конфигурацию
nano .env
```

### 3. Запуск бота

```bash
# Используем удобный скрипт управления
./docker-manage.sh start
```

## ⚙️ Конфигурация

### Переменные окружения (.env)

```bash
# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

# YooKassa Configuration
YOOKASSA_SHOP_ID=your_yookassa_shop_id
YOOKASSA_SECRET_KEY=your_yookassa_secret_key
YOOKASSA_PROVIDER_TOKEN=your_yookassa_provider_token

# WGDashboard Configuration
WG_DASHBOARD_URL=https://your-wg-dashboard.com
WG_DASHBOARD_API_KEY=your_wg_dashboard_api_key

# Logging Configuration
LOG_LEVEL=INFO
```

## 🛠️ Управление ботом

### Использование скрипта управления

```bash
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

### Прямое использование Docker Compose

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

## 📁 Структура проекта

```
wgbot/
├── Dockerfile              # Docker образ
├── docker-compose.yml      # Docker Compose конфигурация
├── .dockerignore           # Игнорируемые файлы для Docker
├── docker-manage.sh        # Скрипт управления
├── env.docker.example      # Пример конфигурации
├── DOCKER_README.md        # Эта документация
├── data/                   # База данных (монтируется)
├── logs/                   # Логи (монтируется)
└── ...                     # Остальные файлы проекта
```

## 🔧 Дополнительные сервисы

### Мониторинг логов (Dozzle)

Для удобного просмотра логов можно запустить Dozzle:

```bash
# Запуск с мониторингом
docker-compose --profile monitoring up -d

# Доступ к веб-интерфейсу
# http://localhost:9999
```

## 📊 Мониторинг

### Health Check

Бот автоматически проверяет свое состояние каждые 30 секунд.

### Логи

Логи сохраняются в директории `./logs/` и доступны через:

```bash
# Просмотр логов
./docker-manage.sh logs

# Или напрямую
docker-compose logs -f wgbot
```

### База данных

База данных SQLite сохраняется в директории `./data/` и монтируется в контейнер.

## 🔒 Безопасность

- Бот запускается под непривилегированным пользователем
- Конфиденциальные данные передаются через переменные окружения
- Файлы конфигурации не включаются в Docker образ

## 🚨 Устранение неполадок

### Проблемы с запуском

1. **Проверьте конфигурацию:**
   ```bash
   cat .env
   ```

2. **Проверьте логи:**
   ```bash
   ./docker-manage.sh logs
   ```

3. **Проверьте статус:**
   ```bash
   ./docker-manage.sh status
   ```

### Проблемы с базой данных

```bash
# Очистка данных (ОСТОРОЖНО!)
docker-compose down
rm -rf data/
./docker-manage.sh start
```

### Проблемы с логами

```bash
# Очистка логов
rm -rf logs/
./docker-manage.sh restart
```

## 📈 Производительность

### Рекомендуемые ресурсы

- **CPU:** 1 vCPU
- **RAM:** 512 MB
- **Диск:** 1 GB

### Оптимизация

- Используйте SSD для базы данных
- Настройте ротацию логов
- Мониторьте использование ресурсов

## 🔄 Обновление

```bash
# Обновление кода
git pull origin dev

# Обновление и перезапуск
./docker-manage.sh update
```

## 📞 Поддержка

При возникновении проблем:

1. Проверьте логи: `./docker-manage.sh logs`
2. Проверьте конфигурацию: `cat .env`
3. Создайте issue в репозитории

---

**Примечание:** Этот Docker setup предназначен для разработки и тестирования. Для продакшена рекомендуется дополнительная настройка безопасности и мониторинга.
