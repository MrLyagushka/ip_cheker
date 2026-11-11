FROM python:3.11-slim

RUN apt-get update && apt-get install -y iputils-ping && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Этап 7 (Б21): версии зависимостей зафиксированы в requirements.txt;
# отдельный COPY-слой даёт кэширование сборки (пересборка кода не
# переускачивает pip).
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Секреты и данные не печатаются в образ — см. .dockerignore;
# .env пробрасывается только через docker-compose (env_file + read-only
# том ./data/.env), данные — через том ./data:/app/data.
COPY *.py /app/
COPY handlers/ /app/handlers/

CMD ["python3", "main.py"]
