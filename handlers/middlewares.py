"""
Внешние outer-middleware (Этап 5).

Б7: раньше пользователи попадали в базу users.json (нужна для массовой
рассылки и статистики админа) ТОЛЬКО через /start. Кто использовал бота без
/start (сразу /scan, /new_ip и т.д.) — не регистрировался и терялся для
рассылок. Теперь каждый входящий update от пользователя с непустым
`from_user` регистрируется автоматически, до диспетчеризации по роутерам.
"""

import logging

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


logger = logging.getLogger(__name__)


class UserRegistrationMiddleware(BaseMiddleware):
    """Outer-middleware: регистрирует автора входящего сообщения/колбэка."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = getattr(event, "from_user", None)
        if user is not None:
            # Импортируем лениво, чтобы модуль можно было тестировать
            # с подменённым config.register_user.
            import config
            try:
                config.register_user(user.id)
            except Exception as e:  # регистрация не должна ронять обработку
                logger.warning("register_user failed: %r", e)
        return await handler(event, data)
