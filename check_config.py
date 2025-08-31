#!/usr/bin/env python3
"""
Скрипт для проверки конфигурации nikonVPN Bot
Проверяет наличие всех необходимых переменных окружения
"""

import os
import sys
from typing import List, Tuple

def check_required_env_vars() -> Tuple[bool, List[str]]:
    """Проверяет наличие всех необходимых переменных окружения"""
    
    required_vars = [
        'TELEGRAM_BOT_TOKEN',
        'WG_DASHBOARD_URL', 
        'WG_DASHBOARD_API_KEY',
        'YOOKASSA_SHOP_ID',
        'YOOKASSA_SECRET_KEY',
        'YOOKASSA_PROVIDER_TOKEN'
    ]
    
    missing_vars = []
    
    for var in required_vars:
        value = os.getenv(var)
        if not value or value.strip() == '':
            missing_vars.append(var)
        elif 'your_' in value.lower() or 'test_' in value.lower():
            print(f"⚠️  WARNING: {var} содержит тестовое значение: {value[:20]}...")
    
    return len(missing_vars) == 0, missing_vars

def validate_telegram_token(token: str) -> bool:
    """Проверяет формат Telegram Bot Token"""
    if not token:
        return False
    
    # Telegram Bot Token должен быть в формате: 123456789:ABCdefGHIjklMNOpqrsTUVwxyz
    parts = token.split(':')
    if len(parts) != 2:
        return False
    
    try:
        bot_id = int(parts[0])
        if bot_id < 100000000:  # Минимальный ID бота
            return False
    except ValueError:
        return False
    
    if len(parts[1]) < 20:  # Минимальная длина токена
        return False
    
    return True

def validate_wg_dashboard_url(url: str) -> bool:
    """Проверяет формат URL WGDashboard"""
    if not url:
        return False
    
    return url.startswith(('http://', 'https://')) and ':' in url

def main():
    """Основная функция проверки"""
    print("🔍 Проверка конфигурации nikonVPN Bot...")
    print("=" * 50)
    
    # Проверка наличия переменных
    all_present, missing = check_required_env_vars()
    
    if not all_present:
        print("❌ ОШИБКА: Отсутствуют обязательные переменные окружения:")
        for var in missing:
            print(f"   - {var}")
        print("\n💡 Создайте файл .env на основе env.docker.example")
        sys.exit(1)
    
    print("✅ Все обязательные переменные окружения присутствуют")
    
    # Детальная проверка
    telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not validate_telegram_token(telegram_token):
        print("❌ ОШИБКА: Неверный формат TELEGRAM_BOT_TOKEN")
        sys.exit(1)
    print("✅ TELEGRAM_BOT_TOKEN имеет правильный формат")
    
    wg_url = os.getenv('WG_DASHBOARD_URL')
    if not validate_wg_dashboard_url(wg_url):
        print("❌ ОШИБКА: Неверный формат WG_DASHBOARD_URL")
        sys.exit(1)
    print("✅ WG_DASHBOARD_URL имеет правильный формат")
    
    # Проверка дополнительных настроек
    log_level = os.getenv('LOG_LEVEL', 'INFO')
    print(f"📊 Уровень логирования: {log_level}")
    
    db_path = os.getenv('DATABASE_PATH', './data/wgbot.db')
    print(f"💾 Путь к базе данных: {db_path}")
    
    print("\n🎉 Конфигурация корректна! Бот готов к запуску.")
    return True

if __name__ == "__main__":
    main()
