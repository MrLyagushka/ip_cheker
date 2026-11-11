import os
import json
import asyncio
from html import escape
from aiogram import Router
# В aiogram 3.15+ класс переименован из TelegramForbiddenException в TelegramForbiddenError;
# fallback на старый алиас совместим с более ранними версиями.
try:
    from aiogram.exceptions import TelegramForbiddenError as TelegramForbiddenException
except ImportError:  # pragma: no cover - старые версии aiogram 3.x
    from aiogram.exceptions import TelegramForbiddenException
from aiogram.types import Message
from aiogram.filters import Command, CommandObject
# Этап 1: единая строгая валидация IPv4 из config (Б4).
# Этап 2: параллельный опрос через ping_many (Б10), crash-guard цикла.
from config import (
    TRACKED_IPS_FILE, active_users, bot,
    ALERT_CHAT_ID, ALERT_THREAD_ID, is_valid_ipv4, ping_many,
)

router = Router()

def load_tracked_ips():
    if os.path.exists(TRACKED_IPS_FILE):
        try:
            with open(TRACKED_IPS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_tracked_ips(data):
    with open(TRACKED_IPS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

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

        await status_msg.edit_text("\n".join(report_lines), parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка внеплановой проверки для {user_id}: {e}")
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
        print(f"Ошибка пинг-пакета для пользователя {user_id_str}: {e}")
        res = {ip: False for ip in ips}
    return {"user_id": int(user_id_str), "results": [(ip, alive) for ip, alive in res.items()]}


def _format_report(results: list[tuple[str, bool]]) -> str:
    lines = ["⏰ <b>Ежечасный автоотчет мониторинга:</b>", "━" * 22]
    alive_count = 0
    for ip, alive in results:
        alive_count += 1 if alive else 0
        lines.append(f"{'🟢' if alive else '🔴'} <code>{escape(str(ip))}</code>")
    dead_count = len(results) - alive_count
    lines.append("")
    lines.append(f"🟢 Доступно: <b>{alive_count}</b> | 🔴 Недоступно: <b>{dead_count}</b>")
    return "\n".join(lines)


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
            print(f"Пропущен пользователь из-за ошибки опроса: {outcome}")
            continue

        user_id = outcome["user_id"]
        results = outcome["results"]
        dead_ips = [ip for ip, alive in results if not alive]

        # 1. Персональный отчёт в личку. Различаем блокировку бота и прочие ошибки.
        try:
            await bot.send_message(user_id, _format_report(results), parse_mode="HTML")
        except TelegramForbiddenException:
            # Пользователь заблокировал бота — отчёт не доставлен, но алерт
            # в групповой чат всё равно уходит (серверы-то лежат).
            print(f"Пользователь {user_id} заблокировал бота — автоотчёт пропущен.")
        except Exception as e:
            print(f"Не удалось отправить автоотчет пользователю {user_id}: {e}")

        # 2. Аварийное уведомление в групповой чат с тегом владельца (Б1).
        if ALERT_CHAT_ID and dead_ips:
            try:
                await bot.send_message(
                    chat_id=ALERT_CHAT_ID,
                    text=_format_alert(user_id, dead_ips),
                    parse_mode="HTML",
                    message_thread_id=ALERT_THREAD_ID,
                )
            except Exception as e:
                print(f"Не удалось отправить алерт в чат {ALERT_CHAT_ID}: {e}")


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
            print(f"Ошибка прохода ежечасного мониторинга (продолжаем): {e}")
