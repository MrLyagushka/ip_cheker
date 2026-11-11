"""Интеграция пинга с заглушкой subprocess — без реальной сети.

async_ping_ip использует asyncio.create_subprocess_exec; в CI ping может быть
недоступен/заблокирован, поэтому подменяем создание процесса фейковым объектом
с предопределённым returncode. Проверяем сквозную семантику: таймаут → False,
kill зависшего процесса, returncode==0 → True, исключение → False (Б19).
"""
import asyncio

import pytest

import config_core as cc


class FakeProc:
    def __init__(self, returncode=0, hang=False):
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    async def wait(self):
        if self._hang:
            await asyncio.sleep(30)     # дольше любого тестового timeout
        return self.returncode

    def kill(self):
        self.killed = True


async def run_with_fake(monkeypatch, proc_factory, **kw):
    throttle = cc.PingThrottle(2)

    async def fake_exec(*args, **kwargs):
        return proc_factory()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return await cc.async_ping_ip("8.8.8.8", throttle_obj=throttle, **kw)


async def test_alive_returncode_zero(test_env, monkeypatch):
    assert await run_with_fake(monkeypatch, lambda: FakeProc(0)) is True


async def test_dead_nonzero_rc(test_env, monkeypatch):
    assert await run_with_fake(monkeypatch, lambda: FakeProc(1)) is False


async def test_timeout_kills_process(test_env, monkeypatch):
    procs = []
    p = FakeProc(hang=True)
    procs.append(p)
    alive = await run_with_fake(monkeypatch, lambda: p, timeout=0.05)
    assert alive is False
    assert p.killed is True             # зависший процесс обязан быть убит


async def test_exception_is_swallowed_false(test_env, monkeypatch):
    throttle = cc.PingThrottle(1)

    async def boom(*a, **k):
        raise FileNotFoundError("ping not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    assert await cc.async_ping_ip("8.8.8.8", throttle_obj=throttle) is False


async def test_ping_many_maps_results(test_env, monkeypatch):
    throttle = cc.PingThrottle(3)
    seq = iter([FakeProc(0), FakeProc(1), FakeProc(0)])

    async def fake_exec(*args, **kwargs):
        return next(seq)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    res = await cc.ping_many(["1.1.1.1", "2.2.2.2", "3.3.3.3"],
                             throttle_obj=throttle)
    assert res == {"1.1.1.1": True, "2.2.2.2": False, "3.3.3.3": True}
