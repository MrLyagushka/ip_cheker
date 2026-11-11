import os
import asyncio
import json
from datetime import datetime
from aiogram import Router
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command, CommandObject
from config import sem, active_users, init_files, RESULT_FILE, HISTORY_FILE

router = Router()

async def async_ping_ip(ip):
    async with sem:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await proc.wait()
            return proc.returncode == 0
        except Exception:
            return False

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

    targets = [t.strip() for t in command.args.split() if t.strip()]
    if not targets:
        await message.answer("❌ Укажите корректные IP-адреса или подсети через пробел.", parse_mode="HTML")
        return

    for target in targets:
        octets = target.split('.')
        if len(octets) != 4 or not all(o.isdigit() for o in octets):
            await message.answer(f"❌ Неверный формат IP или подсети: <code>{target}</code>", parse_mode="HTML")
            return
        if target.endswith('.0.0'):
            await message.answer(f"❌ Подсети формата <code>x.x.0.0</code> запрещены: <code>{target}</code>", parse_mode="HTML")
            return

    active_users.add(user_id)
    
    try:
        init_files()
        display_target = ", ".join(targets) if len(targets) <= 3 else f"{len(targets)} целей"
        status_msg = await message.answer(f"⏳ Сканирование ({display_target}) запущено...", parse_mode="HTML")

        all_ips = []
        for target in targets:
            octets = target.split('.')
            if target.endswith('.0'):
                prefix = f"{octets[0]}.{octets[1]}.{octets[2]}"
                for i in range(256):
                    all_ips.append(f"{prefix}.{i}")
            else:
                all_ips.append(target)

        total_ips = len(all_ips)
        results_list = []
        scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        step_size = max(1, total_ips // 4) if total_ips >= 4 else total_ips
        
        checked_count = 0
        for i in range(0, total_ips, step_size):
            chunk_ips = all_ips[i:i + step_size]
            if not chunk_ips:
                continue
            
            tasks = [async_ping_ip(ip) for ip in chunk_ips]
            chunk_results = await asyncio.gather(*tasks)
            
            for ip, alive in zip(chunk_ips, chunk_results):
                results_list.append((ip, alive))
            
            checked_count = len(results_list)
            percent = int((checked_count / total_ips) * 100)
            
            filled_count = int(10 * percent // 100)
            exec_bar = "🟦" * filled_count + "⬜" * (10 - filled_count)
            
            alive_so_far = sum(1 for _, alive in results_list if alive)
            dead_so_far = checked_count - alive_so_far
            
            ratio_alive = int(10 * alive_so_far // checked_count) if checked_count > 0 else 0
            ratio_bar = "🟩" * ratio_alive + "🟥" * (10 - ratio_alive)
            
            await status_msg.edit_text(
                f"⏳ <b>Сканирование целей:</b> <code>{display_target}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚙️ <b>Индикатор выполнения:</b>\n"
                f"\n<code>{exec_bar}</code> {percent}% ({checked_count}/{total_ips})\n\n"
                f"📈 <b>Индикатор доступности (Online/Offline):</b>\n"
                f"\n<code>{ratio_bar}</code>\n"
                f"🟢 Доступно: <code>{alive_so_far}</code> | 🔴 Недоступно: <code>{dead_so_far}</code>",
                parse_mode="HTML"
            )

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

        with open(RESULT_FILE, "w", encoding="utf-8") as rf:
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
            f"📊 <b>Отчет сканирования:</b> <code>{display_target}</code>\n"
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

        if os.path.exists(RESULT_FILE) and os.path.getsize(RESULT_FILE) > 0:
            file = FSInputFile(RESULT_FILE)
            history_target_label = " ".join(targets)
            await message.answer_document(file, caption=f"📁 Полный отчет аналитики для {history_target_label}")
        else:
            await message.answer("⚠️ Файл результатов пуст.")

    finally:
        active_users.remove(user_id)
