"""
Файл handlers/start.py
Отвечает за первичное знакомство пользователя с ботом, вывод справки по командам 
и проверку прав администратора.
"""
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.enums import ParseMode
# Этап 1: вместо хрупкого `sem._value` (Б13) — публичный API throttle;
# лимит потоков читаем через модуль config, чтобы видеть актуальное
# значение после админской смены «на лету» (Б11).
import config
from config import throttle, ADMIN_ID, register_user

# Создаем изолированный роутер для модуля старта
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message):
    """
    Основная функция-обработчик команды /start.
    Шаги выполнения:
    1. register_user(message.from_user.id) — сохраняет ID пользователя в базу данных (users.json),
       чтобы в дальнейшем администратор мог делать по ним массовые рассылки.
    2. throttle.free — публичное свойство мутируемого ограничителя пингов
       (Б13: отказ от прямого доступа к приватному полю asyncio.Semaphore).
    3. Формирует красивое HTML-сообщение со всеми актуальными командами (/scan, /tree, /new_ip и т.д.).
    4. Проверяет, равен ли ID пользователя константе ADMIN_ID (1424727277). 
       Если да — добавляет в текст подсказку о доступности панели управления (/admin).
    5. Отправляет сообщение в текущий чат/тему форума с поддержкой HTML-разметки.
    """
    # Регистрируем пользователя для системы рассылок
    register_user(message.from_user.id)
    
    # Получаем актуальное количество свободных слотов пинга
    free_slots = throttle.free
    
    text = (
        "Привет! Бот для пинг-сканирования запущен.\n\n"
        f"🟢 Свободных глобальных потоков: <b>{free_slots}/{config.throttle.limit}</b>\n\n"
        "<b>Как пользоваться:</b>\n"
        "• Одиночные IP через пробел: <code>/scan 8.8.8.8 1.1.1.1 9.9.9.9</code>\n"
        "• Подсеть x.x.x.0: <code>/scan 66.86.15.0</code>\n"
        "• Дерево сетей: <code>/tree</code>\n"
        "• Мониторинг IP: <code>/new_ip</code>, <code>/get_ip</code>, <code>/remove_ip</code>, <code>/check_ip</code>\n"
        "❌ Подсети вида <code>x.x.0.0</code> не проверяются.\n\n"
        "ℹ️ <i>Ограничение:</i> 1 активный процесс сканирования на одного пользователя."
    )
    
    # Персональная проверка: если пишет администратор, показываем секретную команду
    if message.from_user.id == ADMIN_ID:
        text += "\n\n🔑 <b>Вам доступна админ-панель:</b> <code>/admin</code>"
        
    await message.answer(text, parse_mode=ParseMode.HTML)
