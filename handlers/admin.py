import os
import json
import asyncio
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.client.session.aiohttp import AiohttpSession
import config
from config import (
    ADMIN_ID, RESULT_FILE, HISTORY_FILE, TRACKED_IPS_FILE, 
    USERS_FILE, save_env_value, bot
)

router = Router()

class AdminStates(StatesGroup):
    waiting_for_concurrency = State()
    waiting_for_proxy = State()
    waiting_for_broadcast = State()

def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика бота", callback_data="admin_stats")],
        [InlineKeyboardButton(text="⚙️ Макс. потоки", callback_data="set_concurrency"),
         InlineKeyboardButton(text="🌐 Прокси", callback_data="set_proxy")],
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="start_broadcast")],
        [InlineKeyboardButton(text="🧹 Очистить историю сетей", callback_data="clear_history")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_admin"),
         InlineKeyboardButton(text="❌ Закрыть", callback_data="close_admin")]
    ])

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У вас нет доступа к этой команде.")
        return

    proxy_status = config.PROXY_URL if config.PROXY_URL else "Отключен (прямое соединение)"
    text = (
        "🛠 <b>Расширенная панель администратора</b>\n\n"
        f"⚙️ Макс. потоков пинга: <code>{config.MAX_CONCURRENT_PINGS}</code>\n"
        f"🟢 Свободных потоков: <code>{config.sem._value}/{config.MAX_CONCURRENT_PINGS}</code>\n"
        f"🌐 Текущий прокси: <code>{proxy_status}</code>\n\n"
        "Выберите действие в меню ниже:"
    )
    await message.answer(text, reply_markup=get_admin_keyboard(), parse_mode="HTML")

@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return

    # Подсчет статистики из файлов
    users_count = 0
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                users_count = len(json.load(f))
        except Exception:
            pass

    tracked_count = 0
    if os.path.exists(TRACKED_IPS_FILE):
        try:
            with open(TRACKED_IPS_FILE, "r", encoding="utf-8") as f:
                tracked_data = json.load(f)
                tracked_count = sum(len(ips) for ips in tracked_data.values())
        except Exception:
            pass

    history_count = 0
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history_count = len(json.load(f))
        except Exception:
            pass

    stats_text = (
        "📊 <b>Статистика системы:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Уникальных пользователей: <code>{users_count}</code>\n"
        f"📍 Всего отслеживаемых IP: <code>{tracked_count}</code>\n"
        f"🌳 Записей в дереве сетей: <code>{history_count}</code>\n"
        f"🔄 Активных потоков занято: <code>{config.MAX_CONCURRENT_PINGS - config.sem._value}</code>"
    )
    await callback.message.edit_text(stats_text, reply_markup=get_admin_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "refresh_admin")
async def cb_refresh_admin(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    proxy_status = config.PROXY_URL if config.PROXY_URL else "Отключен"
    text = (
        "🛠 <b>Расширенная панель администратора</b>\n\n"
        f"⚙️ Макс. потоков пинга: <code>{config.MAX_CONCURRENT_PINGS}</code>\n"
        f"🟢 Свободных потоков: <code>{config.sem._value}/{config.MAX_CONCURRENT_PINGS}</code>\n"
        f"🌐 Текущий прокси: <code>{proxy_status}</code>\n\n"
        "Выберите действие в меню ниже:"
    )
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard(), parse_mode="HTML")
    await callback.answer("Обновлено!")

@router.callback_query(F.data == "close_admin")
async def cb_close_admin(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.delete()

@router.callback_query(F.data == "clear_history")
async def cb_clear_history(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
            
    await callback.answer("🧹 История дерева сетей успешно очищена!", show_alert=True)

@router.callback_query(F.data == "set_concurrency")
async def cb_set_concurrency(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    await callback.message.answer("Введите новое максимальное количество одновременных потоков (целое число, например: <code>5</code>):", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_concurrency)
    await callback.answer()

@router.message(AdminStates.waiting_for_concurrency)
async def process_new_concurrency(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    val = message.text.strip()
    if not val.isdigit() or int(val) < 1:
        await message.answer("❌ Ошибка! Введите целое положительное число.")
        return

    new_limit = int(val)
    config.MAX_CONCURRENT_PINGS = new_limit
    config.sem = asyncio.Semaphore(new_limit)
    save_env_value("MAX_CONCURRENT_PINGS", new_limit)

    await state.clear()
    await message.answer(f"✅ Успешно! Максимальное количество потоков изменено на <b>{new_limit}</b> и сохранено в <code>.env</code>.", parse_mode="HTML")

@router.callback_query(F.data == "set_proxy")
async def cb_set_proxy(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    await callback.message.answer(
        "Введите новый URL прокси (например: <code>socks5://user:pass@ip:port</code>)\n"
        "Или отправьте слово <code>none</code>, чтобы полностью отключить прокси:",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_proxy)
    await callback.answer()

@router.message(AdminStates.waiting_for_proxy)
async def process_new_proxy(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    new_proxy = message.text.strip()
    if new_proxy.lower() in ["none", "off", "откл", "нет"]:
        config.PROXY_URL = ""
        save_env_value("PROXY_URL", "")
        config.session = None
    else:
        config.PROXY_URL = new_proxy
        save_env_value("PROXY_URL", new_proxy)
        config.session = AiohttpSession(proxy=new_proxy)

    bot.session = config.session
    await state.clear()
    
    status_text = f"<b>{config.PROXY_URL}</b>" if config.PROXY_URL else "отключен (прямое соединение)"
    await message.answer(f"✅ Прокси успешно изменен на: {status_text} и сохранен в <code>.env</code>.", parse_mode="HTML")

@router.callback_query(F.data == "start_broadcast")
async def cb_start_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    await callback.message.answer(
        "📢 <b>Режим массовой рассылки</b>\n\n"
        "Отправьте текст или сообщение, которое будет разослано всем пользователям бота:",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.answer()

@router.message(AdminStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    await state.clear()
    
    if not os.path.exists(USERS_FILE):
        await message.answer("❌ База пользователей пуста.")
        return

    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            users = json.load(f)
    except Exception:
            users = []

    if not users:
        await message.answer("❌ Нет зарегистрированных пользователей для рассылки.")
        return

    status_msg = await message.answer(f"⏳ Начинаю рассылку для {len(users)} пользователей...")
    
    success = 0
    failed = 0
    
    for uid in users:
        try:
            await message.send_copy(chat_id=uid)
            success += 1
            await asyncio.sleep(0.05) # Защита от флуд-контроля Telegram
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"• Успешно доставлено: <code>{success}</code>\n"
        f"• Ошибок отправки (заблокировали бота): <code>{failed}</code>",
        parse_mode="HTML"
    )
