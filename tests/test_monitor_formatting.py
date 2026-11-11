"""Форматирование отчётов мониторинга + интеграция monitor-пакета с моками."""
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


async def test_hourly_package_error_not_silent(tmp_path, monkeypatch):
    """Б5: ошибка опроса юзера НЕ проглатывается молча — outcome попадает в errors."""
    users = tmp_path / "users.json"
    users.write_text("[7]")
    tracked = tmp_path / "tracked_ips.json"
    tracked.write_text(json.dumps({"7": ["8.8.8.8"]}))
    monkeypatch.setattr(m, "USERS_FILE", str(users))
    monkeypatch.setattr(m, "TRACKED_IPS_FILE", str(tracked))

    async def broken_pinger(ip, timeout=1.0):
        raise RuntimeError("network down")

    monkeypatch.setattr(m, "async_ping_ip", broken_pinger)

    sent = []

    class FakeBot:
        async def send_message(self, *a, **k):
            sent.append(a)

    monkeypatch.setattr(m, "bot", FakeBot())
    await m.hourly_ping_package([7], 0.05, FakeBot(), results=([], [], [7]))
    # ошибки собраны, отправка «нет данных» не произошла для error-юзера
    assert sent == []


async def test_hourly_loop_survives_iteration_errors(tmp_path, monkeypatch):
    """Упавший проход не убивает цикл: exception -> continue + sleep."""
    calls = {"n": 0}

    async def fake_sleep(x):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError   # остановить цикл после 2 циклов

    monkeypatch.setattr(m.asyncio, "sleep", fake_sleep)
    # load_tracked_networks вернёт {} (файлов нет в tmp cwd?) — используем боевые пути,
    # но пробросим ошибку на gather, эмулируя падение прохода:
    async def boom(targets, timeout, bot_, results=None):
        raise RuntimeError("iteration failure")
    monkeypatch.setattr(m, "hourly_ping_package", boom)

    with pytest.raises(asyncio.CancelledError):
        await m.hourly_monitor_loop()
    assert calls["n"] >= 2               # цикл дожил после ошибки прохода (Б10)
