"""Форматирование отчётов мониторинга + интеграция monitor-пакета с моками.

Обновлено под текущую архитектуру: monitor импортирует пингующие функции из
config (локальное имя `ping_many`), файлы данных читаются через
config_core.load_tracked_ips, доставка — через delivery.safe_send.
"""
import asyncio
import json

import pytest

import config_core as cc
from handlers import monitor as m


def test_report_counts_and_html():
    text = cc.format_monitor_report([("8.8.8.8", True), ("9.9.9.9", False)])
    assert "Доступно: <b>1</b>" in text and "Недоступно: <b>1</b>" in text
    assert "<code>8.8.8.8</code>" in text


def test_alert_escapes_and_mentions():
    text = cc.format_alert(42, ["<bad>"])
    assert "&lt;bad&gt;" in text and "tg://user?id=42" in text


async def test_hourly_tick_error_not_silent(tmp_path, monkeypatch, caplog):
    """Б5: ошибка опроса юзера логируется, а не глотается молча."""
    tracked = tmp_path / "tracked_ips.json"
    tracked.write_text(json.dumps({"7": ["8.8.8.8"]}))
    monkeypatch.setattr(cc, "TRACKED_IPS_FILE", str(tracked))

    async def broken_pinger(targets, timeout=1.0, throttle_obj=None):
        raise RuntimeError("network down")

    # _check_user_ips использует ping_many, импортированный в модуль monitor.
    monkeypatch.setattr(m, "ping_many", broken_pinger)

    sent = []

    class FakeBot:
        async def send_message(self, *a, **k):
            sent.append(a)

    monkeypatch.setattr(m, "bot", FakeBot())
    monkeypatch.setattr(m, "ALERT_CHAT_ID", None)
    # Фоновый тик: pacing-паузы в тесте не нужны.
    monkeypatch.setattr(m, "pace", lambda *a, **k: asyncio.sleep(0))

    delivered = []

    def fake_safe_send(method, *a, **k):
        async def _inner():
            delivered.append(a)          # попытка доставки была (ошибку видно в логе)
            return None                  # имитация провала доставки
        return _inner()

    monkeypatch.setattr(m, "safe_send", fake_safe_send)

    await m.hourly_monitor_tick()

    # Ошибка пинга изолирована и залогирована (_check_user_ips),
    # но отчёт с заведомо ложными 🔴 пользователю НЕ доставляется.
    assert delivered and sent == []


async def test_hourly_tick_delivers_report_with_cleanup_kb(tmp_path, monkeypatch):
    """Автоотчёт доставляется и содержит кнопку «🧹» при наличии dead-IP."""
    tracked = tmp_path / "tracked_ips.json"
    tracked.write_text(json.dumps({"7": ["8.8.8.8", "1.1.1.1"]}))
    monkeypatch.setattr(cc, "TRACKED_IPS_FILE", str(tracked))

    async def fake_pinger(targets, timeout=1.0, throttle_obj=None):
        return {ip: (ip == "8.8.8.8") for ip in targets}

    monkeypatch.setattr(m, "ping_many", fake_pinger)
    monkeypatch.setattr(m, "ALERT_CHAT_ID", None)
    monkeypatch.setattr(m, "pace", lambda *a, **k: asyncio.sleep(0))

    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))
            return object()

    monkeypatch.setattr(m, "bot", FakeBot())

    await m.hourly_monitor_tick()

    assert len(sent) == 1
    chat_id, text, kwargs = sent[0]
    assert chat_id == 7
    assert "🟢" in text and "🔴" in text
    kb = kwargs.get("reply_markup")
    assert kb is not None
    btn = kb.inline_keyboard[0][0]
    assert "Убрать неактивные ip" in btn.text
    assert btn.callback_data.startswith(m.CLEANUP_CB_PREFIX)
    assert len(btn.callback_data.encode()) < 64


async def test_hourly_loop_survives_iteration_errors(tmp_path, monkeypatch):
    """Упавший проход не убивает цикл: exception -> continue + sleep."""
    calls = {"n": 0}

    async def fake_sleep(x):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError   # остановить цикл после 2 циклов

    monkeypatch.setattr(m.asyncio, "sleep", fake_sleep)

    async def boom():
        raise RuntimeError("iteration failure")
    monkeypatch.setattr(m, "hourly_monitor_tick", boom)

    with pytest.raises(asyncio.CancelledError):
        await m.hourly_monitor_loop()
    assert calls["n"] >= 2               # цикл дожил после ошибки прохода (Б10)


async def test_hourly_tick_single_dead_ipv6_still_shows_cleanup_button(tmp_path, monkeypatch):
    """Регрессия бага «под сообщением об автопроверке нет кнопки «Очистить IP»».

    Одиночный не-IPv4 хост (например, IPv6) не кодируется в callback_data —
    раньше build_cleanup_kb возвращал None и автоотчёт уходил без кнопки.
    Теперь для сообщения с одной целью кнопка ставится всегда («🧹 Очистить IP»,
    пустой payload = очистка всего списка нажавшего)."""
    tracked = tmp_path / "tracked_ips.json"
    tracked.write_text(json.dumps({"7": ["2001:db8::1"]}))
    monkeypatch.setattr(cc, "TRACKED_IPS_FILE", str(tracked))

    async def fake_pinger(targets, timeout=1.0, throttle_obj=None):
        return {ip: False for ip in targets}

    monkeypatch.setattr(m, "ping_many", fake_pinger)
    monkeypatch.setattr(m, "ALERT_CHAT_ID", None)
    monkeypatch.setattr(m, "pace", lambda *a, **k: asyncio.sleep(0))

    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))
            return object()

    monkeypatch.setattr(m, "bot", FakeBot())

    await m.hourly_monitor_tick()

    assert len(sent) == 1
    kb = sent[0][2].get("reply_markup")
    assert kb is not None, "кнопка очистки обязательна под одиночным автоотчётом"
    btn = kb.inline_keyboard[0][0]
    assert "Очистить IP" in btn.text
    assert btn.callback_data == m.CLEANUP_CB_PREFIX  # пустой payload


def test_build_cleanup_kb_payload_always_within_callback_limit():
    """Кнопка есть всегда, даже когда ни один адрес не кодируется в payload."""
    assert m.build_cleanup_kb([]) is None                       # мёртвых нет
    assert m.build_cleanup_kb(["не-ip"]) is None                # список целей >1
    kb = m.build_cleanup_kb(["не-ip"], single=True)             # одна цель
    assert kb is not None and len(kb.inline_keyboard[0][0].callback_data.encode()) < 64
    kb2 = m.build_cleanup_kb(["8.8.8.8"])
    assert "Убрать неактивные ip (1)" in kb2.inline_keyboard[0][0].text
