#!/bin/bash

# nikonVPN Bot Docker Management Script
# Usage: ./docker-manage.sh [command]

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

# Function to check if .env file exists
check_env_file() {
    if [ ! -f ".env" ]; then
        print_error ".env file not found!"
        print_status "Please copy env.docker.example to .env and configure it:"
        print_status "cp env.docker.example .env"
        print_status "nano .env"
        exit 1
    fi
}

# Function to create necessary directories
create_directories() {
    print_status "Creating necessary directories..."
    mkdir -p data logs
    print_success "Directories created"
}

# Function to build and start the bot
start() {
    print_status "Starting nikonVPN Bot with Docker Compose..."
    check_env_file
    create_directories
    
    docker-compose up -d
    print_success "Bot started successfully!"
    print_status "View logs with: ./docker-manage.sh logs"
    print_status "Stop bot with: ./docker-manage.sh stop"
}

# Function to stop the bot
stop() {
    print_status "Stopping nikonVPN Bot..."
    docker-compose down
    print_success "Bot stopped successfully!"
}

# Function to restart the bot
restart() {
    print_status "Restarting nikonVPN Bot..."
    docker-compose restart
    print_success "Bot restarted successfully!"
}

# Function to view logs
logs() {
    print_status "Showing bot logs..."
    docker-compose logs -f wgbot
}

# Function to view logs with timestamps
logs_tail() {
    print_status "Showing last 100 lines of bot logs..."
    docker-compose logs --tail=100 -f wgbot
}

# Function to build the image
build() {
    print_status "Building Docker image..."
    docker-compose build --no-cache
    print_success "Docker image built successfully!"
}

# Function to update the bot
update() {
    print_status "Updating nikonVPN Bot..."
    docker-compose pull
    docker-compose build --no-cache
    docker-compose up -d
    print_success "Bot updated successfully!"
}

# Function to show status
status() {
    print_status "Bot status:"
    docker-compose ps
}

# Function to show help
help() {
    echo "nikonVPN Bot Docker Management Script"
    echo ""
    echo "Usage: ./docker-manage.sh [command]"
    echo ""
    echo "Commands:"
    echo "  start      Start the bot"
    echo "  stop       Stop the bot"
    echo "  restart    Restart the bot"
    echo "  build      Build Docker image"
    echo "  update     Update and restart the bot"
    echo "  logs       View bot logs (follow mode)"
    echo "  logs-tail  View last 100 lines of logs"
    echo "  status     Show bot status"
    echo "  help       Show this help message"
    echo ""
    echo "Examples:"
    echo "  ./docker-manage.sh start"
    echo "  ./docker-manage.sh logs"
    echo "  ./docker-manage.sh stop"
}

# Main script logic
case "${1:-help}" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    build)
        build
        ;;
    update)
        update
        ;;
    logs)
        logs
        ;;
    logs-tail)
        logs_tail
        ;;
    status)
        status
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
