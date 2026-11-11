import os
import asyncio
import json
from datetime import datetime
from html import escape
from aiogram import Router
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command, CommandObject
# Этап 1: единый пинг/валидация из config (Б19, Б4); throttle — мутируемый
# семафор, админский лимит действует на этот модуль мгновенно (Б11).
import config
from config import (
    active_users, init_files, HISTORY_FILE,
    async_ping_ip, expand_targets, result_path_for,
)

router = Router()

def save_to_history(target, alive_count, total_ips, percent):
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
    
    # Удаляем старую запись для этой же подсети, если она была
    history = [item for item in history if item["target"] != target]
    history.append({
        "target": target,
        "alive": alive_count,
        "total": total_ips,
        "percent": percent,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=4)

@router.message(Command("scan"))
async def cmd_scan(message: Message, command: CommandObject):
    user_id = message.from_user.id

    if user_id in active_users:
        await message.answer(
            "⚠️ <b>У вас уже запущен активный процесс сканирования!</b>\n"
            "Пожалуйста, дождитесь окончания текущей проверки.",
            parse_mode="HTML"
        )
        return

    if not command.args:
        await message.answer(
            "❌ Вы не указали цель для сканирования!\n\n"
            "Используйте команду с параметрами, например:\n"
            "<code>/scan 201.8.70.166 201.8.70.175</code>\n"
            "или для подсети:\n"
            "<code>/scan 66.86.15.0</code>",
            parse_mode="HTML"
        )
        return

    raw_args = [t.strip() for t in command.args.split() if t.strip()]
    if not raw_args:
        await message.answer("❌ Укажите корректные IP-адреса или подсети через пробел.", parse_mode="HTML")
        return

    # Строгая валидация IPv4 (октеты 0–255) + разворот подсетей x.x.x.0,
    # блокировка x.x.0.0 — единый парсер config.expand_targets (Б4, Б3).
    all_ips, invalid = expand_targets(raw_args)

    if invalid:
        await message.answer(
            "❌ Недопустимые цели (формат IPv4 или запрещённые подсети x.x.0.0): "
            + ", ".join(f"<code>{escape(t)}</code>" for t in invalid),
            parse_mode="HTML",
        )
    if not all_ips:
        return

    active_users.add(user_id)
    
    try:
        init_files()
        display_target = ", ".join(raw_args) if len(raw_args) <= 3 else f"{len(raw_args)} целей"
        safe_display = escape(display_target)
        status_msg = await message.answer(f"⏳ Сканирование ({safe_display}) запущено...", parse_mode="HTML")

        total_ips = len(all_ips)
        results_list = []
        scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Прогресс в реальном времени (Б5): обновляем сообщение
        # не 4 раза по чанкам, а по мере завершения каждого ping
        # (as_completed), но не чаще одного редактирования в ~1.5 сек,
        # чтобы не провоцировать Telegram flood-control.
        results_map: dict[str, bool] = {}
        checked_count = 0
        alive_so_far = 0
        last_edit = 0.0
        started_at = asyncio.get_running_loop().time()

        pending = {asyncio.create_task(async_ping_ip(ip)): ip for ip in all_ips}
        try:
            for fut in asyncio.as_completed(list(pending.keys())):
                ip = pending[fut]
                try:
                    alive = await fut
                except Exception:
                    alive = False
                results_map[ip] = alive
                checked_count += 1
                if alive:
                    alive_so_far += 1

                now = asyncio.get_running_loop().time()
                is_final = checked_count == total_ips
                if is_final or now - last_edit >= 1.5:
                    last_edit = now
                    percent = int((checked_count / total_ips) * 100)
                    filled_count = int(10 * percent // 100)
                    exec_bar = "🔵" * filled_count + "⬜" * (10 - filled_count)
                    dead_so_far = checked_count - alive_so_far
                    ratio_alive = int(10 * alive_so_far // checked_count) if checked_count > 0 else 0
                    ratio_bar = "🟩" * ratio_alive + "🟥" * (10 - ratio_alive)
                    try:
                        await status_msg.edit_text(
                            f"⏳ <b>Сканирование целей:</b> <code>{safe_display}</code>\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            "⚙️ <b>Индикатор выполнения:</b>\n"
                            f"\n<code>{exec_bar}</code> {percent}% ({checked_count}/{total_ips})\n\n"
                            "📈 <b>Индикатор доступности (Online/Offline):</b>\n"
                            f"\n<code>{ratio_bar}</code>\n"
                            f"🟢 Доступно: <code>{alive_so_far}</code> | 🔴 Недоступно: <code>{dead_so_far}</code>",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass  # flood-control / message not modified — не ломаем скан
        finally:
            for t in pending:
                if not t.done():
                    t.cancel()

        # Порядок результатов сохраняем по входному списку целей
        results_list = [(ip, results_map.get(ip, False)) for ip in all_ips]

        # Автоматически собираем результаты в подсети (x.x.x.0)
        subnets_data = {}
        for ip, alive in results_list:
            octets = ip.split('.')
            subnet = f"{octets[0]}.{octets[1]}.{octets[2]}.0"
            if subnet not in subnets_data:
                subnets_data[subnet] = {"alive": 0, "total": 0}
            subnets_data[subnet]["total"] += 1
            if alive:
                subnets_data[subnet]["alive"] += 1

        # Сохраняем каждую получившуюся подсеть в историю отдельно
        for subnet, data in subnets_data.items():
            sub_alive = data["alive"]
            sub_total = data["total"]
            sub_percent = (sub_alive / sub_total) * 100 if sub_total > 0 else 0
            save_to_history(subnet, sub_alive, sub_total, sub_percent)

        alive_count = sum(1 for _, alive in results_list if alive)
        dead_count = total_ips - alive_count
        availability_percent = (alive_count / total_ips) * 100 if total_ips > 0 else 0

        exec_bar = "🟦" * 10
        alive_blocks = int(10 * alive_count // total_ips) if total_ips > 0 else 0
        dead_blocks = 10 - alive_blocks
        ratio_bar = "🟩" * alive_blocks + "🟥" * dead_blocks

        # Пер-юзерский файл отчёта — параллельные сканы
        # разных пользователей больше не перехватывают отчёт друг друга (Б18).
        user_result_file = result_path_for(user_id)
        with open(user_result_file, "w", encoding="utf-8") as rf:
            rf.write("==================================================\n")
            rf.write("             АНАЛИТИКА ПИНГ-СКАНИРОВАНИЯ          \n")
            rf.write("==================================================\n")
            rf.write(f"Цели сканирования:      {display_target}\n")
            rf.write(f"Дата и время проверки:  {scan_time}\n")
            rf.write(f"Всего проверено IP:     {total_ips}\n")
            rf.write(f"Доступно (Online - 'y'): {alive_count}\n")
            rf.write(f"Недоступно (Offline-'n'): {dead_count}\n")
            rf.write(f"Процент доступности:    {availability_percent:.2f}%\n")
            rf.write("--------------------------------------------------\n")
            rf.write("ДЕТАЛЬНЫЙ СТАТУС ПО АДРЕСАМ:\n")
            rf.write("--------------------------------------------------\n")
            for target_ip, alive in results_list:
                res_char = 'y' if alive else 'n'
                rf.write(f"{target_ip} -> {res_char}\n")
            rf.write("==================================================\n")

        final_message = (
            f"📊 <b>Отчет сканирования:</b> <code>{safe_display}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 <b>Доступно:</b> <code>{alive_count}</code> ({availability_percent:.2f}%)\n"
            f"🔴 <b>Недоступно:</b> <code>{dead_count}</code> ({100 - availability_percent:.2f}%)\n"
            f"⏱ <b>Статус:</b> Завершено успешно\n\n"
            f"⚙️ <b>Индикатор выполнения:</b>\n"
            f"\n<code>{exec_bar}</code> 100% (Завершено)\n\n"
            f"📈 <b>Индикатор доступности (Online/Offline):</b>\n"
            f"\n<code>{ratio_bar}</code>"
        )

        await status_msg.edit_text(final_message, parse_mode="HTML")

        if user_result_file.exists() and user_result_file.stat().st_size > 0:
            file = FSInputFile(str(user_result_file))
            history_target_label = " ".join(raw_args)
            await message.answer_document(file, caption=f"📁 Полный отчет аналитики для {escape(history_target_label)}")
        else:
            await message.answer("⚠️ Файл результатов пуст.")

    finally:
        active_users.discard(user_id)
