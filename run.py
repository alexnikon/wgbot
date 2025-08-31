#!/usr/bin/env python3
"""
Точка входа для запуска nikonVPN Telegram Bot в продакшене
"""

import asyncio
import logging
import sys
import os
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent))

from bot import main

def setup_logging():
    """Настройка логирования для продакшена"""
    # Создаем директорию для логов если её нет
    os.makedirs('logs', exist_ok=True)
    
    # Настройка логирования
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('logs/wgbot.log', encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # Настройка логирования для aiogram
    logging.getLogger('aiogram').setLevel(logging.WARNING)
    logging.getLogger('aiohttp').setLevel(logging.WARNING)

def check_environment():
    """Проверка переменных окружения"""
    required_vars = [
        'TELEGRAM_BOT_TOKEN',
        'WG_DASHBOARD_URL',
        'WG_DASHBOARD_API_KEY'
    ]
    
    missing_vars = []
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        print(f"❌ Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}")
        print("📝 Создайте файл .env на основе config/env_example.txt")
        sys.exit(1)
    
    print("✅ Все обязательные переменные окружения настроены")

def create_directories():
    """Создание необходимых директорий"""
    directories = ['data', 'logs']
    
    for directory in directories:
        os.makedirs(directory, exist_ok=True)
        print(f"📁 Директория {directory} готова")

def main_production():
    """Основная функция для продакшена"""
    print("🚀 Запуск nikonVPN Telegram Bot...")
    
    # Проверяем переменные окружения
    check_environment()
    
    # Создаем необходимые директории
    create_directories()
    
    # Настраиваем логирование
    setup_logging()
    
    print("✅ Инициализация завершена")
    print("🤖 Запуск бота...")
    
    try:
        # Запускаем бота
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹️ Получен сигнал остановки")
        print("🛑 Остановка бота...")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        logging.error(f"Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)
    finally:
        print("👋 Бот остановлен")

if __name__ == '__main__':
    main_production()