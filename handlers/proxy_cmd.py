"""Команда /get_proxy — личный SOCKS5-прокси (docker run serjs/go-socks5-proxy).

Пользователь получает готовую команду запуска контейнера со СГЕНЕРИРОВАННЫМИ
логином и паролем. Дальше прокси вида socks5://user:pass@IP:1080 добавляется
в список прокси бота (админ-панель → «🌐 Прокси»); после этого каждый запрос к
Telegram API идёт через случайный прокси из пула, а при ошибке соединения бот
берёт другой (см. proxy_pool.py).
"""

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import proxy_pool

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command("get_proxy"))
async def cmd_get_proxy(message: Message):
    user, password = proxy_pool.generate_credentials()
    cmd = proxy_pool.build_docker_run(user, password)
    text = proxy_pool.format_proxy_command_html(user, password, cmd)
    # Б25-подход не нужен: это ответ на прямой запрос юзера (не фоновый тик),
    # TelegramRetryAfter здесь обработает верхний уровень; но секрет в лог не пишем.
    await message.answer(text, parse_mode="HTML")
    logger.info("Пользователь %s запросил /get_proxy", message.from_user.id)
