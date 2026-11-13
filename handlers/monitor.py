import os
import json
import asyncio
import logging
from html import escape
from aiogram import Router
# В aiogram 3.15+ класс переименован из TelegramForbiddenException в TelegramForbiddenError;
# fallback на старый алиас совместим с более ранними версиями.
try:
    from aiogram.exceptions import TelegramForbiddenError as TelegramForbiddenException
except ImportError:  # pragma: no cover - старые версии aiogram 3.x
    from aiogram.exceptions import TelegramForbiddenException
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
)
from aiogram.filters import Command, CommandObject
# Этап 1: единая строгая валидация IPv4 из config (Б4).
# Этап 2: параллельный опрос через ping_many (Б10), crash-guard цикла.
from config import (
    TRACKED_IPS_FILE, active_users, bot,
    ALERT_CHAT_ID, ALERT_THREAD_ID, is_valid_ipv4, ping_many,
    load_tracked_ips, save_tracked_ips,
    encode_dead_payload, decode_dead_payload, remove_dead_ips,
    format_monitor_report,
    CLEANUP_CB_PREFIX,
)
# Б25: flood-safe доставка для фоновых тиков и массовых рассылок.
from delivery import safe_send, safe_edit, pace

logger = logging.getLogger(__name__)

router = Router()


# ───────────── Кнопка «🧹 Убрать неактивные ip» + callback-хендлер ───────────

def build_cleanup_kb(dead_ips: list[str], single: bool = False) -> InlineKeyboardMarkup | None:
    """Inline-клавиатура «🧹 Убрать неактивные ip» (фича 2).

    None — если мёртвых адресов нет или ни один не кодируется в payload.
    Компактное кодирование (<64 байт, см. config_core.encode_dead_payload):
    каждый IPv4 → 4 байта, base64url без паддинга, префикс CLEANUP_CB_PREFIX.
    Ставится под финальные сообщения /scan, /check_ip и ежечасного автоотчёта;
    под групповые алерты НЕ ставится (алерт видит вся группа, а удалять список
    будет тот, кто нажал).

    Исправление бага «под сообщением об автопроверке нет кнопки»: Telegram
    отбрасывает весь reply_markup целиком, если хотя бы одна callback_data
    длиннее 64 байт (например, в списке мониторинга есть внешний IPv6-хост,
    который не проходит is_valid_ipv4). Поэтому при `single=True` (сообщение
    содержит статусы ровно одного адреса) кнопка ставится всегда — с пустым
    payload, чтобы очистка работала даже когда все цели — не-IPv4.
    """
    payload = encode_dead_payload(dead_ips)
    if not payload and not single:
        return None
    # Счётчик в тексте — по реально кодируемым адресам (payload обрезается до
    # DEAD_IPS_MAX, кнопка не должна обещать больше, чем удалит).
    encoded = decode_dead_payload(payload)
    label = f"🧹 Убрать неактивные ip ({len(encoded)})" if encoded else "🧹 Очистить IP"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=label,
            callback_data=CLEANUP_CB_PREFIX + payload,
        )
    ]])


@router.callback_query(lambda c: c.data and c.data.startswith(CLEANUP_CB_PREFIX))
async def cb_cleanup_dead_ips(callback: CallbackQuery):
    """Обработка нажатия «🧹 Убрать неактивные ip» (фича 2).

    * callback.answer() вызывается ОБЯЗАТЕЛЬНО на всех ветках (в т.ч. при
      ошибке) — иначе у клиента крутится часовой индикатор ожидания;
    * удаление идемпотентно и только по адресам нажимающего пользователя
      (config_core.remove_dead_ips): повторный тап ничего не ломает, чужие
      списки не затрагиваются;
    * пустой payload («🧹 Очистить IP», single-режим build_cleanup_kb) —
      удаляет ВСЕ адреса из списка нажавшего, включая не-IPv4 (например,
      IPv6-хосты), которые нельзя закодировать в callback_data;
    * после обработки клавиатура снимается (edit_reply_markup); если текст
      итогового сообщения недоступен правке (старое/служебное) — это не беда.
    """
    try:
        dead = decode_dead_payload(callback.data[len(CLEANUP_CB_PREFIX):])
        if not dead and callback.data.strip() != CLEANUP_CB_PREFIX:
            await callback.answer("⚠️ Список не распознан — обновите данные командой /get_ip",
                                  show_alert=True)
            return
        if not dead:
            # Кнопка «Очистить IP» без счётчика: чистим весь список юзера.
            dead = list(load_tracked_ips().get(str(callback.from_user.id), []))
            if not dead:
                await callback.answer("ℹ️ Ваш список мониторинга уже пуст.")
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
                return
        removed, skipped = remove_dead_ips(callback.from_user.id, dead)
        if removed:
            note = f"🧹 Убрано из мониторинга: {len(removed)} шт."
            if skipped:
                note += f" (уже отсутствовали: {len(skipped)})"
        else:
            note = "ℹ️ Эти IP уже не состоят в вашем мониторинге."
        # Идемпотентность подтверждается текущим размером списка юзера.
        remaining = len(load_tracked_ips().get(str(callback.from_user.id), []))
        note += f" Всего в мониторинге: {remaining}."
        # Клавиатуру снимаем всегда (иначе повторный тап удалит то, что
        # пользователь добавил обратно руками).
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass  # сообщение слишком старое/не редактируется — не критично
        await callback.answer(note)
    except Exception as e:  # noqa: BLE001 — answer обязателен на любой ветке
        logger.exception("Ошибка обработки callback очистки IP")
        try:
            await callback.answer("❌ Ошибка обработки, попробуйте позже", show_alert=True)
        except Exception:
            pass

@router.message(Command("new_ip"))
async def cmd_new_ip(message: Message, command: CommandObject):
    if not command.args:
        await message.answer(
            "❌ Укажите IP-адрес(а) для добавления в мониторинг!\n\n"
            "Пример:\n<code>/new_ip 8.8.8.8 1.1.1.1 9.9.9.9</code>",
            parse_mode="HTML"
        )
        return

    targets = [t.strip().rstrip(',;') for t in command.args.split() if t.strip()]
    if not targets:
        await message.answer("❌ Укажите корректные IP-адреса через пробел.", parse_mode="HTML")
        return

    user_id_str = str(message.from_user.id)
    tracked = load_tracked_ips()
    user_ips = tracked.get(user_id_str, [])

    added_count = 0
    already_exists = []
    invalid_ips = []

    for ip in targets:
        # Строгая валидация IPv4 через config (октеты 0–255) — Б4:
        # раньше «999.999.999.5» проходил проверку и оседал в мониторинге.
        if not is_valid_ipv4(ip):
            invalid_ips.append(ip)
            continue
        if ip in user_ips:
            already_exists.append(ip)
        else:
            user_ips.append(ip)
            added_count += 1

    if invalid_ips:
        await message.answer(
            "❌ Обнаружены неверные форматы IP: "
            + ", ".join(f"<code>{escape(ip)}</code>" for ip in invalid_ips),
            parse_mode="HTML",
        )

    if not added_count and not already_exists:
        return

    if added_count:
        tracked[user_id_str] = user_ips
        save_tracked_ips(tracked)

    response_parts = []
    if added_count:
        response_parts.append(f"✅ Успешно добавлено: {added_count} шт.")
    if already_exists:
        response_parts.append(f"⚠️ Уже были в списке: {len(already_exists)} шт.")
    
    response_parts.append(f"\n📁 Всего в вашем мониторинге: <b>{len(user_ips)}</b> IP")

    await message.answer("\n".join(response_parts), parse_mode="HTML")

@router.message(Command("get_ip"))
async def cmd_get_ip(message: Message):
    user_id_str = str(message.from_user.id)
    tracked = load_tracked_ips()
    user_ips = tracked.get(user_id_str, [])

    if not user_ips:
        await message.answer(
            "📁 Ваш список отслеживаемых IP пуст.\n"
            "Добавьте адрес командой: <code>/new_ip <ip></code>",
            parse_mode="HTML"
        )
        return

    text = "📋 <b>Ваши отслеживаемые IP-адреса:</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for idx, ip in enumerate(user_ips, 1):
        text += f"{idx}. <code>{ip}</code>\n"
    text += f"\nВсего в мониторинге: <b>{len(user_ips)}</b>"

    await message.answer(text, parse_mode="HTML")

@router.message(Command("remove_ip"))
async def cmd_remove_ip(message: Message, command: CommandObject):
    if not command.args:
        await message.answer(
            "❌ Укажите IP-адрес(а) для удаления!\n\n"
            "Пример:\n<code>/remove_ip 8.8.8.8 1.1.1.1</code>",
            parse_mode="HTML"
        )
        return

    targets = [t.strip() for t in command.args.split() if t.strip()]
    if not targets:
        await message.answer("❌ Укажите корректные IP-адреса через пробел для удаления.", parse_mode="HTML")
        return

    user_id_str = str(message.from_user.id)
    tracked = load_tracked_ips()
    user_ips = tracked.get(user_id_str, [])

    if not user_ips:
        await message.answer("📁 Ваш список отслеживаемых IP и так пуст.", parse_mode="HTML")
        return

    removed_count = 0
    not_found = []

    for ip in targets:
        if ip in user_ips:
            user_ips.remove(ip)
            removed_count += 1
        else:
            not_found.append(ip)

    tracked[user_id_str] = user_ips
    save_tracked_ips(tracked)

    response_parts = []
    if removed_count > 0:
        response_parts.append(f"🗑 Удалено IP из мониторинга: <b>{removed_count}</b>")
    if not_found:
        response_parts.append(f"⚠️ Не найдены в вашем списке: {', '.join([f'<code>{ip}</code>' for ip in not_found])}")
    
    response_parts.append(f"📁 Осталось в мониторинге: <b>{len(user_ips)}</b> IP")

    await message.answer("\n".join(response_parts), parse_mode="HTML")

@router.message(Command("check_ip"))
async def cmd_check_ip(message: Message):
    user_id = message.from_user.id

    # Атомарная проверка+занятие флага через ActiveUserRegistry (Б6);
    # фоновый ежечасный мониторинг этот реестр не трогает (Б22).
    if not active_users.try_acquire(user_id):
        await message.answer(
            "⚠️ <b>У вас уже запущен активный процесс сканирования или проверки!</b>\n"
            "Пожалуйста, дождитесь окончания текущей операции.",
            parse_mode="HTML"
        )
        return

    user_id_str = str(user_id)
    tracked = load_tracked_ips()
    user_ips = tracked.get(user_id_str, [])

    if not user_ips:
        await message.answer(
            "❌ У вас нет добавленных IP для мониторинга.\n"
            "Добавьте их с помощью команды <code>/new_ip <ip></code>",
            parse_mode="HTML"
        )
        return

    status_msg = await message.answer(f"⏳ Внеплановая проверка {len(user_ips)} ваших IP запущена...", parse_mode="HTML")

    try:
        # Параллельный опрос через единый пул throttle (Б10/Б19):
        # ping_many возвращает {ip: alive}, ошибки отдельных пингов изолированы.
        res_map = await ping_many(list(user_ips))
        results = [(ip, res_map.get(ip, False)) for ip in user_ips]

        alive_count = sum(1 for _, alive in results if alive)
        dead_count = len(results) - alive_count

        report_lines = [
            f"📊 <b>Внеплановый отчет проверки:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        ]
        for ip, alive in results:
            status_icon = "🟢" if alive else "🔴"
            report_lines.append(f"{status_icon} <code>{escape(ip)}</code>")

        report_lines.append(f"\n🟢 Доступно: <b>{alive_count}</b> | 🔴 Недоступно: <b>{dead_count}</b>")

        # Фича 2: кнопка «🧹 Убрать неактивные ip» под финальным отчётом.
        # single=True: при одной цели кнопка («Очистить IP») ставится всегда —
        # иначе для не-IPv4-хоста (например, IPv6) payload пуст и Telegram-
        # сообщение уходит без кнопки очистки.
        kb = build_cleanup_kb([ip for ip, alive in results if not alive],
                              single=len(results) == 1)
        # Б25: edit с ретраем по RetryAfter; fallback — новое сообщение.
        if not await safe_edit(status_msg, "\n".join(report_lines), reply_markup=kb):
            await safe_send(message.answer, "\n".join(report_lines),
                            parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.exception("Ошибка внеплановой проверки для %s", user_id)
        try:
            await status_msg.edit_text("❌ <b>Проверка завершилась с ошибкой.</b>", parse_mode="HTML")
        except Exception:
            pass
    finally:
        active_users.release(user_id)

async def _check_user_ips(user_id_str: str, ips: list[str]) -> dict:
    """Параллельный опрос IP одного пользователя (Б10).

    gather по всем адресам сразу; конкурентность ограничена общим
    config.throttle (семафором), поэтому очередь Telegram не забивается.
    Ошибки валидации/пинга отдельного IP не должны ронять весь цикл.
    """
    try:
        res = await ping_many(list(ips))
    except Exception as e:
        logger.exception("Ошибка пинг-пакета для пользователя %s", user_id_str)
        res = {ip: False for ip in ips}
    return {"user_id": int(user_id_str), "results": [(ip, alive) for ip, alive in res.items()]}


def _format_report(results: list[tuple[str, bool]]) -> str:
    """Делегат в config_core.format_monitor_report (импортирован как
    _format_report) — убран дубль текста отчёта (принцип из PROJECT_CONTEXT:
    перед правкой определить реально используемый путь)."""
    return format_monitor_report(results)


def _format_alert(user_id: int, dead_ips: list[str]) -> str:
    """Формирует текст аварийного алерта с тегом владельца (Б1).

    Исправление критического бага: раньше `alert_text` был строкой, к которой
    вызывался `.append()` — это кидало AttributeError на каждой итерации и
    полностью убивало фоновый цикл мониторинга. Теперь используется список строк.
    """
    mention = f'<a href="tg://user?id={user_id}">Владелец</a>'
    alert_lines = [
        "🚨 <b>Внимание! Обнаружены недоступные хосты!</b>",
        f"👤 Владелец: {mention}",
        "━" * 22,
    ]
    alert_lines += [f"🔴 <code>{escape(ip)}</code>" for ip in dead_ips]
    return "\n".join(alert_lines)


async def hourly_monitor_tick():
    """Один проход ежечасного мониторинга (без while/sleep)."""
    tracked = load_tracked_ips()
    if not tracked:
        return

    # Параллельный опрос всех пользователей сразу (Б10): ошибки отдельных
    # пользователей изолированы через return_exceptions=True.
    tasks = [_check_user_ips(uid, ips) for uid, ips in tracked.items() if ips]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    for outcome in outcomes:
        if isinstance(outcome, Exception):
            logger.error("Пропущен пользователь из-за ошибки опроса: %r", outcome)
            continue

        user_id = outcome["user_id"]
        results = outcome["results"]
        dead_ips = [ip for ip, alive in results if not alive]

        # 1. Персональный отчёт в личку. Различаем блокировку бота и прочие ошибки.
        # Фича 2: под автоотчётом — кнопка «🧹 Убрать неактивные ip».
        # Б25: safe_send c ретраем по RetryAfter; cap=300 — фоновый тик не
        # должен блокироваться длинными паузами (лимит Telegram ~1 msg/sec на
        # чат здесь не актуален: лички разные, но общий лимит 30 msg/sec есть).
        kb = build_cleanup_kb(dead_ips, single=len(results) == 1)
        try:
            # safe_send сам перехватывает TelegramRetryAfter/прочие
            # TelegramAPIError и возвращает None; TelegramForbiddenError
            # (юзер заблокировал бота) — подкласс TelegramAPIError, поэтому
            # проверяем результат и различаем причину по типу последней ошибки.
            sent = await safe_send(
                bot.send_message, user_id, _format_report(results),
                parse_mode="HTML", reply_markup=kb, cap=300.0,
            )
            if sent is None:
                logger.info("Автоотчёт пользователю %s не доставлен "
                            "(блокировка/flood) — алерт в чат всё равно уйдёт.",
                            user_id)
        except Exception as e:
            logger.warning("Не удалось отправить автоотчет пользователю %s: %r", user_id, e)

        # 2. Аварийное уведомление в групповой чат с тегом владельца (Б1).
        # КНОПКУ «🧹» НЕ ставим: алерт видят все участники группы, а удаление
        # списков привязано к нажимающему (иначе любой участник мог бы давить
        # чужие кнопки). Б25: группа ~300 юзеров + мониторинг пробивают лимит
        # ~1 msg/sec на чат — поэтому pacing между рассылками и ретрай по
        # retry_after с капом 300 сек (фоновый тик не должен ждать вечно).
        if ALERT_CHAT_ID and dead_ips:
            await pace()
            try:
                await safe_send(
                    bot.send_message,
                    chat_id=ALERT_CHAT_ID,
                    text=_format_alert(user_id, dead_ips),
                    parse_mode="HTML",
                    message_thread_id=ALERT_THREAD_ID,
                    cap=300.0,
                )
            except Exception as e:
                logger.warning("Не удалось отправить алерт в чат %s: %r", ALERT_CHAT_ID, e)


async def hourly_monitor_loop():
    """Фоновый цикл: раз в 3600 сек. Crash-guard (Б10/Б22): любая ошибка
    одного прохода не должна останавливать задачу целиком."""
    while True:
        await asyncio.sleep(3600)
        try:
            await hourly_monitor_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Ошибка прохода ежечасного мониторинга (продолжаем)")
