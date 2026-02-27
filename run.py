#!/usr/bin/env python3
"""
Production entrypoint for nikonVPN Telegram Bot.
"""

import asyncio
import logging
import sys
import os
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent))

from bot import main

def setup_logging():
    """Configure logging for production."""
    # Create logs directory if it does not exist
    os.makedirs('logs', exist_ok=True)
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('logs/wgbot.log', encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # Reduce noise from aiogram/aiohttp
    logging.getLogger('aiogram').setLevel(logging.WARNING)
    logging.getLogger('aiohttp').setLevel(logging.WARNING)

def check_environment():
    """Validate required environment variables."""
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
        print(f"❌ Missing required environment variables: {', '.join(missing_vars)}")
        print("📝 Create a .env file based on config/env_example.txt")
        sys.exit(1)
    
    print("✅ Required environment variables are set")

def create_directories():
    """Create required directories."""
    directories = ['data', 'logs']
    
    for directory in directories:
        os.makedirs(directory, exist_ok=True)
        print(f"📁 Directory {directory} is ready")

def main_production():
    """Main production runner."""
    print("🚀 Starting nikonVPN Telegram Bot...")
    
    # Validate environment variables
    check_environment()
    
    # Create required directories
    create_directories()
    
    # Configure logging
    setup_logging()
    
    print("✅ Initialization complete")
    print("🤖 Starting bot...")
    
    try:
        # Run bot
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹️ Stop signal received")
        print("🛑 Stopping bot...")
    except Exception as e:
        print(f"❌ Critical error: {e}")
        logging.error(f"Critical error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        print("👋 Bot stopped")

if __name__ == '__main__':
    main_production()
