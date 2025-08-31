# Используем официальный Python образ на Alpine
FROM python:3.11-alpine

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем системные зависимости для Alpine
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    openssl-dev \
    && rm -rf /var/cache/apk/*

# Копируем файл зависимостей
COPY requirements.txt .

# Устанавливаем Python зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY . .

# Создаем директорию для логов
RUN mkdir -p /app/logs

# Создаем пользователя для безопасности
RUN adduser -D -s /bin/sh wgbot
RUN chown -R wgbot:wgbot /app
USER wgbot

# Открываем порт (если понадобится для health checks)
EXPOSE 8000

# Команда запуска
CMD ["python", "run.py"]
