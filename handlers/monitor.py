import os
import json
import asyncio
from html import escape
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandObject
# Этап 1: единая строгая валидация IPv4 из config (Б4).
from config import (
    TRACKED_IPS_FILE, active_users, async_ping_ip, bot,
    ALERT_CHAT_ID, ALERT_THREAD_ID, is_valid_ipv4,
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

    if user_id in active_users:
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

    active_users.add(user_id)
    status_msg = await message.answer(f"⏳ Внеплановая проверка {len(user_ips)} ваших IP запущена...", parse_mode="HTML")

    try:
        tasks = [async_ping_ip(ip) for ip in user_ips]
        results_bools = await asyncio.gather(*tasks)
        results = list(zip(user_ips, results_bools))

        alive_count = sum(1 for _, alive in results if alive)
        dead_count = len(results) - alive_count

        report_lines = [
            f"📊 <b>Внеплановый отчет проверки:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        ]
        for ip, alive in results:
            status_icon = "🟢" if alive else "🔴"
            report_lines.append(f"{status_icon} <code>{ip}</code>")
        
        report_lines.append(f"\n🟢 Доступно: <b>{alive_count}</b> | 🔴 Недоступно: <b>{dead_count}</b>")

        await status_msg.edit_text("\n".join(report_lines), parse_mode="HTML")
    finally:
        active_users.remove(user_id)

# Фоновая задача автоматического мониторинга раз в час (3600 секунд)
async def hourly_monitor_loop():
    while True:
        await asyncio.sleep(3600)
        tracked = load_tracked_ips()
        if not tracked:
            continue

        for user_id_str, ips in tracked.items():
            if not ips:
                continue
            user_id = int(user_id_str)
            
            results = []
            for ip in ips:
                alive = await async_ping_ip(ip)
                results.append((ip, alive))

            alive_count = sum(1 for _, alive in results if alive)
            dead_count = len(results) - alive_count

            # 1. Отправляем полный ежечасный отчет пользователю в личку
            report_lines = [
                f"⏰ <b>Ежечасный автоотчет мониторинга:</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            ]
            for ip, alive in results:
                status_icon = "🟢" if alive else "🔴"
                report_lines.append(f"{status_icon} <code>{ip}</code>")
            
            report_lines.append(f"\n🟢 Доступно: <b>{alive_count}</b> | 🔴 Недоступно: <b>{dead_count}</b>")

            try:
                await bot.send_message(user_id, "\n".join(report_lines), parse_mode="HTML")
            except Exception as e:
                print(f"Не удалось отправить автоотчет пользователю {user_id}: {e}")

            # 2. Если задан ALERT_CHAT_ID, проверяем наличие упавших IP и тегаем юзера в теме чата
            if ALERT_CHAT_ID:
                dead_ips = [ip for ip, alive in results if not alive]
                if dead_ips:
                    user_mention = f"<a href='tg://user?id={user_id}'>Владелец</a>"
                    alert_text = (
                        f"🚨 <b>Внимание! Обнаружены недоступные хосты!</b>\n"
                        f"👤 Владелец: {user_mention}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    )
                    for dip in dead_ips:
                        alert_text.append(f"🔴 <code>{dip}</code>")
                    
                    kwargs = {"parse_mode": "HTML"}
                    if ALERT_THREAD_ID is not None:
                        kwargs["message_thread_id"] = ALERT_THREAD_ID

                    try:
                        await bot.send_message(
                            chat_id=ALERT_CHAT_ID,
                            text="\n".join(alert_text),
                            **kwargs
                        )
                    except Exception as e:
                        print(f"Не удалось отправить алерт в чат {ALERT_CHAT_ID}: {e}")
