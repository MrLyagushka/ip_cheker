FROM python:3.11-slim

RUN apt-get update && apt-get install -y iputils-ping && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir aiogram aiohttp-socks

# Копируем всю структуру проекта
COPY . /app/

CMD ["python3", "main.py"]
