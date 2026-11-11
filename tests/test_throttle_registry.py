"""Юнит-тесты PingThrottle (Б11, Б13) и ActiveUserRegistry (Б6, Б22)."""
import asyncio

import pytest

import config_core as cc


async def test_resize_increase_frees_slots():
    t = cc.PingThrottle(2)
    async with t():
        assert t.free == 1
        t.resize(5)                      # админ поднял лимит во время скана
        assert t.limit == 5 and t.free == 4
        async with t():
            pass
    assert t.free == 5


async def test_resize_decrease_takes_free_slots():
    t = cc.PingThrottle(5)
    t.resize(3)
    assert t.free == 3
    async with t():
        assert t.busy == 1


async def test_busy_never_negative_when_shrunk_below_active():
    t = cc.PingThrottle(4)
    async with t():
        t.resize(1)                      # уменьшение ниже числа активных
        assert t.busy >= 1               # busy не уходит в минус (Б13)
        async with t():                  # новый слот всё равно доступен
            pass


async def test_concurrency_capped_by_limit():
    t = cc.PingThrottle(2)
    active = peak = 0

    async def worker():
        nonlocal active, peak
        async with t():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(10)))
    assert peak == 2


async def test_registry_acquire_release_and_stale_ttl():
    reg = cc.ActiveUserRegistry(ttl=0.05)
    assert reg.try_acquire(1) is True
    assert reg.try_acquire(1) is False       # второй процесс того же юзера — отказ
    await asyncio.sleep(0.06)                 # ttl истёк — запись устарела
    assert reg.is_active(1) is False          # stale-очистка
    assert reg.try_acquire(1) is True         # слот снова свободен
    reg.release(1)
    assert len(reg) == 0
