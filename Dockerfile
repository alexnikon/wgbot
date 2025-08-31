# Multi-stage build для оптимизации размера
FROM python:3.11-alpine AS builder

# Устанавливаем системные зависимости для сборки
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    openssl-dev \
    && rm -rf /var/cache/apk/*

# Копируем файл зависимостей
COPY requirements.txt .

# Устанавливаем Python зависимости в виртуальное окружение
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Финальный образ
FROM python:3.11-alpine AS runtime

# Копируем виртуальное окружение из builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Устанавливаем только runtime зависимости
RUN apk add --no-cache \
    ca-certificates \
    && rm -rf /var/cache/apk/*

# Устанавливаем рабочую директорию
WORKDIR /app

# Создаем пользователя для безопасности
RUN adduser -D -s /bin/sh wgbot

# Копируем исходный код
COPY --chown=wgbot:wgbot . .

# Создаем директории для данных и логов
RUN mkdir -p /app/logs /app/data && \
    chown -R wgbot:wgbot /app

# Переключаемся на непривилегированного пользователя
USER wgbot

# Открываем порт (если понадобится для health checks)
EXPOSE 8000

# Команда запуска
CMD ["python", "run.py"]
