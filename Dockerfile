FROM python:3.11-slim

RUN apt-get update && apt-get install -y iputils-ping && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir aiogram aiohttp-socks

# Секреты и данные не печатаются в образ — см. .dockerignore;
# .env пробрасывается через docker-compose (env_file), данные — через volumes
COPY *.py /app/
COPY handlers/ /app/handlers/
COPY .env.example /app/

CMD ["python3", "main.py"]
