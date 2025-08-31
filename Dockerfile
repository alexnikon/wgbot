# Используем официальный Python образ
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Копируем файл зависимостей
COPY requirements.txt .

# Устанавливаем Python зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY . .

# Создаем директорию для логов
RUN mkdir -p /app/logs

# Создаем пользователя для безопасности
RUN useradd --create-home --shell /bin/bash wgbot
RUN chown -R wgbot:wgbot /app
USER wgbot

# Открываем порт (если понадобится для health checks)
EXPOSE 8000

# Команда запуска
CMD ["python", "run.py"]
