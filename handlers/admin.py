import os
import json
import asyncio
import logging
from aiogram import Bot, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, CommandObject
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
# Этап 1: admin НЕ трогает config.sem напрямую — вся работа с лимитом идёт
# через мутируемый throttle (Б11), статистика потоков — через публичные
# throttle.free / throttle.busy вместо приватного sem._value (Б13).
# RESULT_FILE больше не импортируется: отчёты пер-юзерские (Б18).
import config
from config import (
    ADMIN_ID, HISTORY_FILE, TRACKED_IPS_FILE,
    USERS_FILE, save_env_value, bot, throttle,
)
# Пул прокси: рандомный выбор на каждый запрос + ротация при ошибке соединения.
import proxy_pool

logger = logging.getLogger(__name__)

router = Router()

# ─────────────── Этап 6: FSM админки только в личных сообщениях ───────────────
# Хендлеры состояний waiting_for_* регистрируются глобально; без этого guard
# текст, набранный админом в группе/канале во время ожидания ввода, попал бы
# в админский парсер (Б16). Приватность проверяется по chat.type.
def _is_private(message: Message) -> bool:
    return message.chat.type == "private"


async def _close_state_if_admin(message: Message, state: FSMContext):
    """Сбрасывает FSM-состояние, если сообщение прислал админ вне лички."""
    if message.from_user and message.from_user.id == ADMIN_ID:
        await state.clear()


# ─────────────── Этап 6: управление задачей рассылки (отмена) ───────────────
_broadcast_task: asyncio.Task | None = None


@router.callback_query(F.data == "cancel_broadcast")
async def cb_cancel_broadcast(callback: CallbackQuery):
    global _broadcast_task
    if callback.from_user.id != ADMIN_ID:
        # Б15: отказ ВСЕГДА сопровождается callback.answer(), иначе кнопка
        # у перехватившего «висит» с индикатором загрузки.
        await callback.answer("Нет доступа!", show_alert=True)
        return
    if _broadcast_task and not _broadcast_task.done():
        _broadcast_task.cancel()
        await callback.answer("🛑 Рассылка отменена", show_alert=True)
    else:
        await callback.answer("Активная рассылка не найдена", show_alert=True)

def _threads_line() -> str:
    """Актуальная строка о потоках для панели (лимит читаем из throttle)."""
    return (
        f"⚙️ Макс. потоков пинга: <code>{throttle.limit}</code>\n"
        f"🟢 Свободных потоков: <code>{throttle.free}/{throttle.limit}</code>"
    )


def _proxy_line() -> str:
    """Строка о прокси в панели: активный (из БД) + размер пула ротации."""
    active = proxy_pool.mask_url(config.PROXY_URL) if config.PROXY_URL \
        else "Отключен (прямое соединение)"
    n = len(proxy_pool.pool.urls)
    return (f"🌐 Текущий прокси: <code>{active}</code>\n"
            f"🔄 Прокси в пуле: <code>{n}</code> "
            f"(БД <code>proxies.db</code>, выбор случайного на каждый запрос, "
            f"ротация при ошибке)")


class AdminStates(StatesGroup):
    waiting_for_concurrency = State()
    waiting_for_proxy = State()
    waiting_for_broadcast = State()

def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика бота", callback_data="admin_stats")],
        [InlineKeyboardButton(text="⚙️ Макс. потоки", callback_data="set_concurrency"),
         InlineKeyboardButton(text="🌐 Прокси", callback_data="set_proxy")],
        [InlineKeyboardButton(text="🧾 Пул прокси", callback_data="proxy_list")],
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

    text = (
        "🛠 <b>Расширенная панель администратора</b>\n\n"
        f"{_threads_line()}\n"
        f"{_proxy_line()}\n\n"
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
        f"🔄 Активных потоков занято: <code>{throttle.busy}/{throttle.limit}</code>"
    )
    await callback.message.edit_text(stats_text, reply_markup=get_admin_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "refresh_admin")
async def cb_refresh_admin(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    proxy_line = _proxy_line()
    text = (
        "🛠 <b>Расширенная панель администратора</b>\n\n"
        f"{_threads_line()}\n"
        f"{proxy_line}\n\n"
        "Выберите действие в меню ниже:"
    )
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard(), parse_mode="HTML")
    await callback.answer("Обновлено!")

@router.callback_query(F.data == "close_admin")
async def cb_close_admin(callback: CallbackQuery):
    # Б15: отказ ВСЕГДА с callback.answer() — иначе у не-админа кнопка
    # «зависает» с индикатором загрузки (было: голый return без answer).
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()

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
    # Б16: FSM админки работает только в личных сообщениях — ввод в группе
    # игнорируется (состояние админа сбрасывается, чтобы не «залипать»).
    if not _is_private(message):
        await _close_state_if_admin(message, state)
        return
    # Б16: guard на message.text is None (стикер/фото/аудио роняли парсер
    # с AttributeError: 'NoneType' object has no attribute 'strip').
    if message.text is None:
        await message.answer("❌ Ошибка! Отправьте число текстом.")
        return

    val = message.text.strip()
    if not val.isdigit() or int(val) < 1:
        await message.answer("❌ Ошибка! Введите целое положительное число.")
        return

    new_limit = int(val)
    # Б11 + Этап 6: лимит меняется ВНУТРИ мутируемого throttle.resize(), а не
    # пересозданием `config.sem` — все модули (scan/monitor), держащие ссылку
    # на один и тот же объект, начинают работать с новым лимитом мгновенно.
    config.throttle.resize(new_limit)
    save_env_value("MAX_CONCURRENT_PINGS", new_limit)

    await state.clear()
    await message.answer(f"✅ Успешно! Максимальное количество потоков изменено на <b>{new_limit}</b> и сохранено в <code>.env</code>.", parse_mode="HTML")

@router.callback_query(F.data == "set_proxy")
async def cb_set_proxy(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    pool_n = len(proxy_pool.pool.urls)
    pool_line = (f"\nВ пуле сейчас <b>{pool_n}</b> прокси — отправленный URL будет\n"
                 "<b>добавлен</b> к ним (не заменит их).\n" if pool_n else "")
    await callback.message.answer(
        "Введите новый URL прокси (например: <code>socks5://user:pass@ip:port</code>)\n"
        "Или отправьте слово <code>none</code>, чтобы полностью отключить прокси.\n"
        "Слово <code>list</code> — показать пул, <code>clear</code> — очистить его."
        + pool_line,
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_proxy)
    await callback.answer()


def _proxy_list_kb() -> InlineKeyboardMarkup | None:
    """Кнопки удаления прокси из пула (callback_data ≤64 байта)."""
    rows = []
    for url in proxy_pool.pool.urls:
        data = f"px_del|{url}"
        if len(data.encode("utf-8")) > 64:
            continue  # Telegram не примет — молча пропускаем (малоявимо: URL короткий)
        rows.append([InlineKeyboardButton(text=f"🗑 {proxy_pool.mask_url(url)}",
                                          callback_data=data)])
    rows.append([InlineKeyboardButton(text="⬅️ В админку", callback_data="refresh_admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _proxy_list_text() -> str:
    urls = proxy_pool.pool.urls
    active = proxy_pool.proxy_db.get_active()
    lines = ["🧾 <b>Пул прокси</b> (хранится в БД <code>proxies.db</code>; бот "
             "выбирает случайный на каждый запрос,\n"
             "при ошибке соединения Telegram — переключается на другой):\n"]
    if not urls:
        lines.append("Пусто. Добавьте прокси через «🌐 Прокси» или /add_proxy.")
    for i, u in enumerate(urls, 1):
        mark = " ⭐ активный" if u == active else ""
        lines.append(f"{i}. <code>{proxy_pool.mask_url(u)}</code>{mark}")
    return "\n".join(lines)


@router.callback_query(F.data == "proxy_list")
async def cb_proxy_list(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    await callback.message.answer(await _proxy_list_text(),
                                  reply_markup=_proxy_list_kb(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("px_del|"))
async def cb_proxy_delete(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    url = callback.data.split("|", 1)[1]
    removed = await proxy_pool.pool.remove(url)
    # Активный PROXY_URL убрали из пула — переключаемся на прямой режим,
    # иначе config.PROXY_URL продолжит показывать несуществующий прокси.
    if removed and config.PROXY_URL == url:
        old = config.set_proxy(None)   # сам синхронизирует пул и чистит .env
        if old is not None:
            try:
                await old.close()
            except Exception as e:
                logger.warning("Не удалось закрыть старую сессию: %r", e)
        removed_note = "Это был активный прокси — переключено на прямое соединение."
    else:
        removed_note = ""
    text = await _proxy_list_text()
    if removed:
        text = f"✅ Удалён: <code>{proxy_pool.mask_url(url)}</code>\n{removed_note}\n\n" + text
        try:
            await callback.message.edit_text(text, reply_markup=_proxy_list_kb(),
                                             parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, reply_markup=_proxy_list_kb(),
                                          parse_mode="HTML")
        await callback.answer("Прокси удалён")
    else:
        await callback.answer("Такого прокси в пуле нет")


@router.message(AdminStates.waiting_for_proxy)
async def process_new_proxy(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    # Б16: FSM только в личных сообщениях.
    if not _is_private(message):
        await _close_state_if_admin(message, state)
        return
    # Б16: guard на не-текстовые сообщения (стикер/фото роняли .strip()).
    if message.text is None:
        await message.answer("❌ Ошибка! Отправьте URL прокси текстом или слово <code>none</code>.", parse_mode="HTML")
        return

    new_proxy = message.text.strip()
    low = new_proxy.lower()
    if low in ["none", "off", "откл", "нет"]:
        new_proxy = None
    elif low == "list":
        await state.clear()
        await message.answer(await _proxy_list_text(),
                             reply_markup=_proxy_list_kb(), parse_mode="HTML")
        return
    elif low == "clear":
        await state.clear()
        # «clear» чистит только БД-пул; если активный прокси был — переключаемся
        # на прямое соединение (set_proxy(None) сам снимет active в БД).
        old = config.set_proxy(None) if config.PROXY_URL else None
        await proxy_pool.pool.replace_all([])   # синхронизирует кэш пула с БД
        if old is not None:
            try:
                await old.close()
            except Exception as e:
                logger.warning("Не удалось закрыть старую сессию: %r", e)
        await message.answer("🧹 Пул прокси очищен (БД), прокси отключён "
                             "(прямое соединение).")
        return
    # Этап 1+6: вся атомарная логика смены прокси в config.set_proxy():
    # обновляет PROXY_URL, строит НОВУЮ валидную AiohttpSession (никогда не
    # None — Б12), подменяет базовую сессию живого бота и делает URL активным
    # в БД proxies.db (в .env больше НЕ пишет).
    old_session = config.set_proxy(new_proxy)
    if old_session is not None:
        try:
            # Б12: СТАРАЯ сессия закрывается КОРРЕКТНО, асинхронно через
            # await close() aiogram (закрывает aiohttp-connector и пул
            # соединений). Прежний «синхронный force_close» убран.
            await old_session.close()
        except Exception as e:
            logger.warning("Не удалось закрыть старую сессию: %r", e)
    added = False
    if new_proxy:
        # Новый прокси ВСЕГДА попадает в список пула (БД): бот начнёт выбирать его
        # случайно наряду с остальными (требование: «заносится в список прокси»).
        # set_proxy() уже добавил/активировал его в proxies.db; add() вернёт
        # False как дубликат — это нормально, сверяемся со списком.
        await proxy_pool.pool.add(new_proxy)
        added = new_proxy in proxy_pool.pool.urls
    await state.clear()

    status_text = f"<b>{proxy_pool.mask_url(config.PROXY_URL)}</b>" if config.PROXY_URL \
        else "отключен (прямое соединение)"
    n = len(proxy_pool.pool.urls)
    note = " Добавлен в список прокси." if added else ""
    await message.answer(
        f"✅ Прокси успешно изменен на: {status_text} и сохранен в <b>БД прокси</b> "
        f"(<code>proxies.db</code>).{note}\n🔄 В пуле: {n} шт.",
        parse_mode="HTML")


@router.message(Command("add_proxy"))
async def cmd_add_proxy(message: Message, command: CommandObject):
    """Админ: добавить прокси в пул, НЕ меняя активный. /add_proxy socks5://user:pass@ip:1080"""
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    url = (command.args or "").strip().split()[0] if command.args else ""
    if not url:
        await message.answer(
            "❌ Укажите URL прокси!\nПример:\n"
            "<code>/add_proxy socks5://user:pass@1.2.3.4:1080</code>",
            parse_mode="HTML")
        return
    if "://" not in url:
        await message.answer(
            "❌ Неверный формат. Ожидается <code>socks5://user:pass@host:port</code> "
            "(или http/socks4).", parse_mode="HTML")
        return
    added = await proxy_pool.pool.add(url)
    if not added:
        await message.answer("⚠️ Такой прокси уже есть в списке.")
        return
    await message.answer(
        f"✅ Прокси <code>{proxy_pool.mask_url(url)}</code> добавлен в список.\n"
        f"🔄 В пуле: {len(proxy_pool.pool.urls)} шт. — бот выбирает случайный "
        "на каждый запрос, при ошибке Telegram переключается на другой.")

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
    """Триггер рассылки: запускает фоновую задачу (Б14).

    Раньше вся рассылка крутилась синхронно внутри хендлера — бот не мог
    отвечать другим пользователям во время неё, не было отмены и обработки
    RetryAfter. Теперь тяжёлая работа в _broadcast_worker(), здесь только
    проверки и запуск задачи с кнопкой «🛑 Отменить».
    """
    global _broadcast_task
    if message.from_user.id != ADMIN_ID:
        return
    # Б16: FSM админки только в личных сообщениях (текст/медиа в группе
    # во время ожидания не должны уходить в рассылку).
    if not _is_private(message):
        await _close_state_if_admin(message, state)
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

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛑 Отменить рассылку", callback_data="cancel_broadcast")
    ]])
    status_msg = await message.answer(
        f"⏳ Начинаю рассылку для {len(users)} пользователей...",
        reply_markup=cancel_kb,
    )

    _broadcast_task = asyncio.create_task(_broadcast_worker(message, users, status_msg))


async def _safe_edit(msg, text: str):
    """Редактирование статусного сообщения без падения рассылки."""
    try:
        await msg.edit_text(text, parse_mode="HTML")
    except Exception:
        pass  # сообщение удалено/устарело/flood — не критично для рассылки


async def _broadcast_worker(source_message: Message, users: list, status_msg):
    """Фоновая рассылка copy-сообщения всем зарегистрированным (Б14).

    - TelegramRetryAfter (флуд-контроль): ждём e.retry_after секунд и
      ПОВТОРЯЕМ попытку по тому же получателю (раньше ошибка молча
      прерывала цикл); суммарная пауза ограничена 5 минутами;
    - TelegramForbiddenError: пользователь заблокировал бота — учитывается
      отдельно, рассылка продолжается;
    - троттлинг обновления статуса ~1 раз/сек + финальный итог;
    - asyncio.CancelledError: корректная отмена кнопкой «🛑 Отменить».
    """
    success = blocked = failed = 0
    total = len(users)
    last_edit = 0.0
    waited_total = 0.0
    MAX_RETRY_WAIT = 300.0  # суммарный пауз на флуд-wait, чтобы не зависнуть

    try:
        for idx, uid in enumerate(users, start=1):
            while True:
                try:
                    await source_message.send_copy(chat_id=uid)
                    success += 1
                    break
                except TelegramRetryAfter as e:
                    pause = float(getattr(e, "retry_after", 1) or 1)
                    waited_total += pause
                    if waited_total > MAX_RETRY_WAIT:
                        failed += 1
                        break
                    await asyncio.sleep(pause + 0.5)
                except TelegramForbiddenError:
                    blocked += 1  # пользователь заблокировал бота
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failed += 1
                    break

            now = asyncio.get_running_loop().time()
            if now - last_edit >= 1.0 or idx == total:  # троттлинг статусов ~1 Гц
                last_edit = now
                await _safe_edit(
                    status_msg,
                    f"📢 Рассылка: <b>{idx}/{total}</b>\n"
                    f"✅ Доставлено: <code>{success}</code> · "
                    f"⛔ Блок: <code>{blocked}</code> · "
                    f"❌ Ошибок: <code>{failed}</code>",
                )
            await asyncio.sleep(0.05)  # базовый pacing между отправками

        await _safe_edit(
            status_msg,
            "✅ <b>Рассылка завершена!</b>\n\n"
            f"• Успешно доставлено: <code>{success}</code>\n"
            f"• Заблокировали бота: <code>{blocked}</code>\n"
            f"• Прочих ошибок: <code>{failed}</code>",
        )
    except asyncio.CancelledError:
        done = success + blocked + failed
        await _safe_edit(
            status_msg,
            "🛑 <b>Рассылка отменена администратором.</b>\n\n"
            f"• Обработано: <code>{done}</code> из <code>{total}</code>\n"
            f"• Успешно доставлено: <code>{success}</code>\n"
            f"• Заблокировали бота: <code>{blocked}</code>\n"
            f"• Прочих ошибок: <code>{failed}</code>",
        )
        raise
    except Exception as e:
        await _safe_edit(status_msg, f"❌ Рассылка аварийно остановлена: <code>{e!r}</code>")
