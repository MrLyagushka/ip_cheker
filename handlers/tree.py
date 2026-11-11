import os
import json
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.enums import ParseMode
from config import HISTORY_FILE

router = Router()

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

    tree_text = "🌳 <b>Дерево отсканированных целей и сетей:</b>\n"
    tree_text += "└── 📁 <b>История проверок</b>\n"

    for idx, item in enumerate(history):
        target = item["target"]
        percent = item["percent"]
        alive = item["alive"]
        total = item["total"]
        scan_time = item["time"]

        if percent == 100.0:
            status_icon = "🟢"
        elif percent == 0.0:
            status_icon = "🔴"
        else:
            status_icon = "🔵"

        branch = "└──" if idx == len(history) - 1 else "├──"
        tree_text += f"    {branch} {status_icon} <code>{target}</code> — <b>{percent:.2f}%</b> ({alive}/{total}) <i>[{scan_time}]</i>\n"

    tree_text += "\n<b>Легенда статусов:</b>\n"
    tree_text += "🟢 Полностью доступна (незаблочена)\n"
    tree_text += "🔵 Частично доступна (наполовину заблочена)\n"
    tree_text += "🔴 Полностью недоступна (фул заблочена)"

    await message.answer(tree_text, parse_mode=ParseMode.HTML)
