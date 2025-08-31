#!/bin/bash

# nikonVPN Telegram Bot Deployment Script
# Использование: ./deploy.sh

set -e

echo "🚀 Деплой nikonVPN Telegram Bot..."

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Функция для вывода сообщений
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Проверка прав root
if [[ $EUID -eq 0 ]]; then
   log_error "Не запускайте этот скрипт от имени root!"
   exit 1
fi

# Переменные
PROJECT_DIR="/opt/wgbot"
SERVICE_USER="wgbot"
SERVICE_FILE="wgbot.service"

log_info "Создание пользователя для сервиса..."
if ! id "$SERVICE_USER" &>/dev/null; then
    sudo useradd -r -s /bin/false -d "$PROJECT_DIR" "$SERVICE_USER"
    log_info "Пользователь $SERVICE_USER создан"
else
    log_info "Пользователь $SERVICE_USER уже существует"
fi

log_info "Создание директории проекта..."
sudo mkdir -p "$PROJECT_DIR"
sudo chown "$SERVICE_USER:$SERVICE_USER" "$PROJECT_DIR"

log_info "Копирование файлов проекта..."
sudo cp -r . "$PROJECT_DIR/"
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$PROJECT_DIR"

log_info "Создание виртуального окружения..."
cd "$PROJECT_DIR"
sudo -u "$SERVICE_USER" python3 -m venv venv

log_info "Установка зависимостей..."
sudo -u "$SERVICE_USER" "$PROJECT_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$PROJECT_DIR/venv/bin/pip" install -r requirements.txt

log_info "Создание необходимых директорий..."
sudo -u "$SERVICE_USER" mkdir -p "$PROJECT_DIR/data" "$PROJECT_DIR/logs"

log_info "Настройка прав доступа..."
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$PROJECT_DIR"
sudo chmod +x "$PROJECT_DIR/run.py"

log_info "Копирование systemd сервиса..."
sudo cp "$SERVICE_FILE" "/etc/systemd/system/"

log_info "Перезагрузка systemd..."
sudo systemctl daemon-reload

log_info "Включение автозапуска..."
sudo systemctl enable "$SERVICE_FILE"

log_warn "ВНИМАНИЕ: Не забудьте настроить файл .env в $PROJECT_DIR"
log_warn "Скопируйте config/env_example.txt в .env и заполните переменные"

echo ""
log_info "Деплой завершен!"
echo ""
echo "Следующие шаги:"
echo "1. Настройте .env файл: sudo nano $PROJECT_DIR/.env"
echo "2. Запустите сервис: sudo systemctl start wgbot"
echo "3. Проверьте статус: sudo systemctl status wgbot"
echo "4. Просмотрите логи: sudo journalctl -u wgbot -f"
echo ""
log_info "Готово! 🎉"
