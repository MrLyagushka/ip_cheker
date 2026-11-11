import os
import json
import html
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from config import HISTORY_FILE

router = Router()

# Б23: лимит сообщения Telegram — 4096 символов; паккуем с запасом.
MAX_BLOCK_LEN = 3500


def _type_label(item: dict) -> str:
    """Метка типа записи истории (Б9). Legacy-записи без поля type — сети."""
    return "🌐 сеть" if item.get("type", "subnet") == "subnet" else "📌 хост"


def _status_icon(percent: float) -> str:
    """Б8: пороги цветов через epsilon-сравнение.

    🟢 — полностью доступна (>= 99.999%), 🔴 — полностью недоступна (<= 0.001%),
    🔵 — всё, что между (0.1%..99.9% по ТЗ). Float-арифметика percent = alive/total*100
    может дать 99.99999999999999 или 1e-14 вместо точных границ, поэтому сравнение
    идёт с допуском, а не `== 100.0` / `== 0`.
    """
    if percent >= 100.0 - 0.001:   # >= 99.999
        return "🟢"
    if percent <= 0.001:            # <= 0.001
        return "🔴"
    return "🔵"


def _format_entry(idx: int, history_len: int, item: dict) -> str:
    target = html.escape(str(item["target"]))  # экранирование целей (как в scan)
    percent = float(item["percent"])
    alive = item["alive"]
    total = item["total"]
    scan_time = html.escape(str(item["time"]))
    branch = "└──" if idx == history_len - 1 else "├──"
    return (
        f"    {branch} {_status_icon(percent)} <code>{target}</code> "
        f"[{_type_label(item)}] — <b>{percent:.2f}%</b> ({alive}/{total}) "
        f"<i>[{scan_time}]</i>\n"
    )


def build_tree_blocks(history: list) -> list:
    """Собирает дерево и пакует вывод в блоки <= MAX_BLOCK_LEN символов (Б23)."""
    header = "🌳 <b>Дерево отсканированных целей и сетей:</b>\n"
    tree_head = header + "└── 📁 <b>История проверок</b>\n"
    legend = (
        "\n<b>Легенда статусов:</b>\n"
        "🟢 Полностью доступна (незаблочена)\n"
        "🔵 Частично доступна (наполовину заблочена)\n"
        "🔴 Полностью недоступна (фул заблочена)"
    )

    n = len(history)
    blocks = []
    current = tree_head

    for idx, item in enumerate(history):
        entry = _format_entry(idx, n, item)
        if current != tree_head and len(current) + len(entry) > MAX_BLOCK_LEN:
            blocks.append(current.rstrip("\n"))
            current = "└── 📁 <b>История проверок (продолжение)</b>\n"
        current += entry

    if len(current) + len(legend) > MAX_BLOCK_LEN:
        # Легенда не влезает — выносим её отдельным финальным сообщением.
        blocks.append(current.rstrip("\n"))
        blocks.append(legend.lstrip("\n"))
    else:
        blocks.append((current + legend).rstrip("\n"))
    return blocks


@router.message(Command("tree"))
async def cmd_tree(message: Message):
    if not os.path.exists(HISTORY_FILE):
        await message.answer("📁 История сканирований пуста. Проверьте что-нибудь через `/scan`.", parse_mode=ParseMode.HTML)
        return

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        history = []

    if not history:
        await message.answer("📁 История сканирований пуста.", parse_mode=ParseMode.HTML)
        return

    # Б23: раньше весь вывод шёл одним сообщением и падал при >4096 символов.
    for block in build_tree_blocks(history):
        try:
            await message.answer(block, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            break  # дальше слать бессмысленно (флуд-контроль/заблокирован чат)
