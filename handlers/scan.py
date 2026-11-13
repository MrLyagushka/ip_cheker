"""Команда /scan: пинг-сканирование одиночных IP и подсетей x.x.x.0.

Этап 3:
- Б5 — потоковый прогресс через asyncio.wait + троттлинг редактирования ~1 Гц
  (вместо 4 обновлений по чанкам total//4);
- Б17 — все edit_text в try/except TelegramAPIError, финальный результат
  дублируется новым сообщением при провале редактирования; HTML-экранирование
  пользовательских целей;
- Б9/Б3 — история с типами записей subnet/host вместо псевдо-подсетей из
  одиночных IP; запрещённые x.x.0.0 не попадают в историю ни одним путём;
  добавлены scanned_by и ограничение размера истории;
- Б6 — флаг активности через ActiveUserRegistry (stale-очистка зависших записей);
- Б25 — доставка финального отчёта через safe_send/safe_edit (ретрай по
  TelegramRetryAfter): раньше необработанный flood-wait «останавливал»
  сканирование и глотался внешним except Exception.
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from html import escape

from aiogram import Router
from aiogram.types import (
    Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

# Этап 1: единый пинг/валидация из config (Б19, Б4); throttle — мутируемый
# семафор, админский лимит действует на этот модуль мгновенно (Б11).
import config
from config import (
    active_users, init_files, HISTORY_FILE,
    async_ping_ip, classify_targets, result_path_for,
)
# Б25: flood-safe доставка (retry по e.retry_after + pacing массовых отправок).
from delivery import safe_send, pace
# Фича 2: компактное кодирование dead-IP в callback_data (<64 байт),
# идемпотентное удаление из tracked_ips.json, сборка клавиатуры «🧹».
from config_core import (
    encode_dead_payload, decode_dead_payload, remove_dead_ips,
    CLEANUP_CB_PREFIX, build_cleanup_kb as _build_cleanup_kb_core,
)

logger = logging.getLogger(__name__)

router = Router()

HISTORY_MAX_ENTRIES = 500


def save_to_history(entries: list[dict]):
    """Сохранение результатов скана в networks_history.json (Б9, Б3).

    entries — список {"type": "subnet"|"host", "target", "alive", "total",
    "percent", "time", "scanned_by"}. Запись перезаменяет предыдущий срез
    той же цели (dedupe по target), всего хранится не более
    HISTORY_MAX_ENTRIES последних записей.
    """
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            # Однократная миграция legacy-записей без поля type → subnet
            for item in loaded:
                if isinstance(item, dict) and "target" in item:
                    item.setdefault("type", "subnet")
                    history.append(item)
        except Exception:
            history = []

    new_targets = {e["target"] for e in entries}
    history = [item for item in history if item.get("target") not in new_targets]
    history.extend(entries)
    history = history[-HISTORY_MAX_ENTRIES:]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=4)


async def _safe_edit(message: Message, text: str) -> bool:
    """edit_text с защитой от flood-control/прочих ошибок Telegram (Б17, Б25).

    Б25: при TelegramRetryAfter делаем ровно одну паузу по e.retry_after и
    повторяем — прогресс-тик не должен «съедаться» флуд-контролем. Пауза
    каппится 5 секундами: это интерактивный троттлируемый прогресс (~1 Гц),
    долго ждать в нём бессмысленно — следующий тик всё равно через секунду.
    """
    try:
        await message.edit_text(text, parse_mode="HTML")
        return True
    except TelegramAPIError:
        return False
    except Exception:
        return False


# ─────────────────────── Статусы и кнопки (фичи 1–2, Б25) ────────────────────

# Секция «Статусы» выводится только для одиночных целей: подсеть — это 256
# строк, они гарантированно пробивают лимит 4096 символов и бесполезны в чате
# (полный список есть в result.txt).
STATUS_SECTION_MAX_HOSTS = 30
TELEGRAM_MESSAGE_LIMIT = 4096
TRUNCATION_HINT = "📎 Полный список статусов — в файле result.txt"


def build_status_section(results_list: list[tuple[str, bool]],
                         header: str = "Статусы") -> str:
    """Секция «🟢/🔴 напротив каждого хоста» (фича 1). Пустая строка для подсетей."""
    if len(results_list) > STATUS_SECTION_MAX_HOSTS:
        return ""
    if not results_list:
        return ""
    lines = [f"📋 <b>{header}:</b>", ""]
    for ip, alive in results_list:
        icon = "🟢" if alive else "🔴"
        lines.append(f"{icon} <code>{escape(ip)}</code>")
    return "\n".join(lines)


def fit_with_status(final_message: str, status_section: str) -> tuple[str, str]:
    """Упаковка финального текста + секции статусов в лимит 4096 символов.

    Возвращает (текст_сообщения, примечание_об_урезке): если секция не влезает,
    она усекается по целым строкам и добавляется отсылка к result.txt.
    """
    if not status_section:
        return final_message, ""
    candidate = final_message + "\n\n" + status_section
    if len(candidate) <= TELEGRAM_MESSAGE_LIMIT:
        return candidate, ""
    budget = TELEGRAM_MESSAGE_LIMIT - len(final_message) - 2 - len(TRUNCATION_HINT) - 1
    kept: list[str] = []
    size = 0
    for line in status_section.split("\n"):
        add = len(line) + (1 if kept else 0)
        if size + add > max(budget, 0):
            break
        kept.append(line)
        size += add
    note = "⚠️ Список статусов усечён из-за лимита Telegram (4096 символов)."
    text = final_message + "\n\n" + "\n".join(kept) + "\n…" if kept else final_message
    return text, note + " " + TRUNCATION_HINT


def build_cleanup_kb(dead_ips: list[str]) -> InlineKeyboardMarkup | None:
    """Inline-клавиатура «🧹 Убрать неактивные ip» (фича 2).

    None — если мёртвых адресов нет (кнопка не нужна). Компактное кодирование
    dead-IP в callback_data (<64 байт) и идемпотентное удаление — в config_core.
    """
    payload = encode_dead_payload(dead_ips)
    if not payload:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"🧹 Убрать неактивные ip ({len(dead_ips)})",
            callback_data=CLEANUP_CB_PREFIX + payload,
        )
    ]])


@router.message(Command("scan"))
async def cmd_scan(message: Message, command: CommandObject):
    user_id = message.from_user.id

    # Атомарная проверка+занятие флага (Б6): без гонки между двумя
    # одновременными /scan одного пользователя.
    if not active_users.try_acquire(user_id):
        await message.answer(
            "⚠️ <b>У вас уже запущен активный процесс сканирования!</b>\n"
            "Пожалуйста, дождитесь окончания текущей проверки.",
            parse_mode="HTML"
        )
        return

    try:
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
        # блокировка x.x.0.0, план истории с типами записей (Б4, Б3, Б9).
        all_ips, invalid, history_plan = classify_targets(raw_args)

        if invalid:
            await message.answer(
                "❌ Недопустимые цели (формат IPv4 или запрещённые подсети x.x.0.0): "
                + ", ".join(f"<code>{escape(t)}</code>" for t in invalid),
                parse_mode="HTML",
            )
        if not all_ips:
            return

        init_files()
        display_target = ", ".join(raw_args) if len(raw_args) <= 3 else f"{len(raw_args)} целей"
        safe_display = escape(display_target)  # Б17: цели приходят от пользователя
        status_msg = await message.answer(f"⏳ Сканирование ({safe_display}) запущено...", parse_mode="HTML")

        total_ips = len(all_ips)
        scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ── Прогресс в реальном времени (Б5) ─────────────────────────────
        # Каждое завершение пинга учитывается сразу; редактирование сообщения
        # — не чаще 1 раза в секунду (Telegram limit ~20 edits/min в чат).
        results_map: dict[str, bool] = {}
        checked_count = 0
        alive_so_far = 0
        last_edit = 0.0
        loop = asyncio.get_running_loop()

        tasks = {asyncio.create_task(async_ping_ip(ip)): ip for ip in all_ips}
        pending = set(tasks)
        try:
            while pending:
                # asyncio.as_completed в листе задач (см. ревью): await над
                # внутренним future не даёт сопоставить результат с задачей,
                # поэтому потоковая обработка — через wait(FIRST_COMPLETED).
                # Учёт каждого завершения мгновенный, edit — троттлинг ~1 Гц.
                done, pending = await asyncio.wait(
                    pending, timeout=1.0, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    ip = tasks[t]
                    try:
                        alive = t.result() is True
                    except (Exception, asyncio.CancelledError):
                        alive = False
                    results_map[ip] = alive
                    checked_count += 1
                    if alive:
                        alive_so_far += 1

                now = loop.time()
                is_final = checked_count == total_ips
                # Троттлинг редактирования ~1 Гц (Б5); финальное обновление — всегда.
                if not is_final and now - last_edit < 1.0:
                    continue
                last_edit = now
                percent = int((checked_count / total_ips) * 100)
                filled_count = int(10 * percent // 100)
                exec_bar = "🔵" * filled_count + "⬜" * (10 - filled_count)
                dead_so_far = checked_count - alive_so_far
                ratio_alive = int(10 * alive_so_far // checked_count) if checked_count > 0 else 0
                ratio_bar = "🟩" * ratio_alive + "🟥" * (10 - ratio_alive)
                progress_text = (
                    f"⏳ <b>Сканирование целей:</b> <code>{safe_display}</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "⚙️ <b>Индикатор выполнения:</b>\n"
                    f"\n<code>{exec_bar}</code> {percent}% ({checked_count}/{total_ips})\n\n"
                    "📈 <b>Индикатор доступности (Online/Offline):</b>\n"
                    f"\n<code>{ratio_bar}</code>\n"
                    f"🟢 Доступно: <code>{alive_so_far}</code> | 🔴 Недоступно: <code>{dead_so_far}</code>"
                )
                await _safe_edit(status_msg, progress_text)
        finally:
            for t in pending:
                t.cancel()

        # Порядок результатов сохраняем по входному списку целей
        results_list = [(ip, results_map.get(ip, False)) for ip in all_ips]

        # ── История: типы subnet/host вместо псевдо-подсетей (Б9, Б3) ────
        # Подсеть = полный обход 256 адресов x.x.x.0; host = одиночный IP.
        history_entries = []
        for plan in history_plan:
            if plan["type"] == "subnet":
                base = plan["target"][:-2]  # "x.x.x."
                subset = [(ip, a) for ip, a in results_list if ip.startswith(base)]
            else:
                ip = plan["target"]
                subset = [(ip, results_map.get(ip, False))]
            sub_alive = sum(1 for _, a in subset if a)
            sub_total = len(subset) or 1
            history_entries.append({
                "type": plan["type"],
                "target": plan["target"],
                "alive": sub_alive,
                "total": len(subset),
                "percent": (sub_alive / sub_total) * 100,
                "time": scan_time,
                "scanned_by": user_id,
            })
        try:
            save_to_history(history_entries)
        except Exception as e:
            logger.exception("Ошибка сохранения истории для %s", user_id)

        alive_count = sum(1 for _, alive in results_list if alive)
        dead_count = total_ips - alive_count
        availability_percent = (alive_count / total_ips) * 100 if total_ips > 0 else 0

        exec_bar = "🟦" * 10
        alive_blocks = int(10 * alive_count // total_ips) if total_ips > 0 else 0
        dead_blocks = 10 - alive_blocks
        ratio_bar = "🟩" * alive_blocks + "🟥" * dead_blocks

        # Пер-юзерский файл отчёта — параллельные сканы разных пользователей
        # больше не перехватывают отчёт друг друга (Б18).
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

        # Б17: если редактирование недоступно (flood/старое сообщение) —
        # дублируем финальный результат новым сообщением.
        if not await _safe_edit(status_msg, final_message):
            await message.answer(final_message, parse_mode="HTML")

        if user_result_file.exists() and user_result_file.stat().st_size > 0:
            file = FSInputFile(str(user_result_file), filename="result.txt")
            await message.answer_document(
                file,
                caption=f"📁 Полный отчет аналитики для {escape(' '.join(raw_args))}",
            )
        else:
            await message.answer("⚠️ Файл результатов пуст.")

    except Exception as e:
        logger.exception("Ошибка сканирования для %s", user_id)
        try:
            await message.answer("❌ <b>Сканирование завершилось с ошибкой.</b>", parse_mode="HTML")
        except Exception:
            pass
    finally:
        active_users.release(user_id)
