#!/bin/bash

# nikonVPN Bot Production Deployment Script
# Usage: ./deploy-prod.sh

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to check if .env file exists and is valid
check_config() {
    print_status "Проверка конфигурации..."
    
    if [ ! -f ".env" ]; then
        print_error ".env file not found!"
        print_status "Please create .env file based on env.docker.example:"
        print_status "cp env.docker.example .env"
        print_status "nano .env"
        exit 1
    fi
    
    # Проверяем конфигурацию через Python скрипт
    if python3 check_config.py; then
        print_success "Конфигурация корректна"
    else
        print_error "Конфигурация содержит ошибки"
        exit 1
    fi
}

# Function to create necessary directories
create_directories() {
    print_status "Создание необходимых директорий..."
    mkdir -p data logs
    chmod 755 data logs
    print_success "Директории созданы"
}

# Function to pull latest image
pull_image() {
    print_status "Загрузка последней версии образа..."
    docker pull alexnikon/wgbot:latest
    print_success "Образ загружен"
}

# Function to stop existing containers
stop_containers() {
    print_status "Остановка существующих контейнеров..."
    docker-compose down || true
    print_success "Контейнеры остановлены"
}

# Function to start production containers
start_containers() {
    print_status "Запуск продакшен контейнеров..."
    docker-compose up -d
    print_success "Контейнеры запущены"
}

# Function to show status
show_status() {
    print_status "Статус контейнеров:"
    docker-compose ps
    
    print_status "Использование ресурсов:"
    docker stats --no-stream --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}" wgbot
}

# Function to show logs
show_logs() {
    print_status "Последние логи:"
    docker-compose logs --tail=20 wgbot
}

# Function to show help
help() {
    echo "nikonVPN Bot Production Deployment Script"
    echo ""
    echo "Usage: ./deploy-prod.sh [command]"
    echo ""
    echo "Commands:"
    echo "  deploy     Full production deployment"
    echo "  update     Update and restart containers"
    echo "  status     Show container status"
    echo "  logs       Show container logs"
    echo "  stop       Stop containers"
    echo "  help       Show this help message"
    echo ""
    echo "Examples:"
    echo "  ./deploy-prod.sh deploy"
    echo "  ./deploy-prod.sh update"
    echo "  ./deploy-prod.sh status"
}

# Main deployment function
deploy() {
    print_status "Начинаем продакшен деплой nikonVPN Bot..."
    
    check_config
    create_directories
    pull_image
    stop_containers
    start_containers
    
    print_success "Продакшен деплой завершен!"
    print_status "Проверьте статус: ./deploy-prod.sh status"
    print_status "Просмотр логов: ./deploy-prod.sh logs"
}

# Update function
update() {
    print_status "Обновление nikonVPN Bot..."
    
    check_config
    pull_image
    stop_containers
    start_containers
    
    print_success "Обновление завершено!"
}

# Main script logic
case "${1:-deploy}" in
    deploy)
        deploy
        ;;
    update)
        update
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs
        ;;
    stop)
        stop_containers
        ;;
    help|--help|-h)
        help
        ;;
    *)
        print_error "Unknown command: $1"
        help
        exit 1
        ;;
esac
