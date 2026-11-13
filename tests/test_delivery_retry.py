"""Б25: retry-логика safe_send/safe_edit при TelegramRetryAfter (flood-control).

Покрывает требования из плана:
* ретрай строго по e.retry_after (с капом и суммарным лимитом ожидания);
* «message is not modified» (400) — НЕ ошибка, safe_edit возвращает True;
* после исчерпания попыток safe_send → None, safe_edit → False (fallback
  для вызывающего кода);
* пауза реально равна retry_after + запас и каппится параметром cap.
"""
import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

import delivery
from delivery import _retry_pause, safe_edit, safe_send


class FakeMethod:
    """Джин метода Telegram: бросает переданные исключения по порядку."""

    def __init__(self, errors, result="ok"):
        self._errors = list(errors)
        self.calls = 0
        self.result = result

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self.result


def make_retry_after(retry_after: int) -> TelegramRetryAfter:
    method = type("M", (), {"__name__": "sendMessage"})()
    return TelegramRetryAfter(method=method, message=f"Too Many Requests: retry after {retry_after}",
                              retry_after=retry_after)


@pytest.fixture()
def no_sleep(monkeypatch):
    """Не тратить реальное время на asyncio.sleep в тестах; логируем паузы."""
    pauses = []

    async def fake_sleep(sec):
        pauses.append(sec)

    monkeypatch.setattr(delivery.asyncio, "sleep", fake_sleep)
    return pauses


def test_safe_send_retries_on_retryafter(no_sleep):
    m = FakeMethod([make_retry_after(3)])
    res = asyncio.run(safe_send(m))
    assert res == "ok"
    assert m.calls == 2                    # первый упал, второй прошёл
    assert no_sleep == [3.5]               # retry_after + 0.5 запас


def test_safe_send_gives_up_after_max_attempts(no_sleep):
    m = FakeMethod([make_retry_after(1)] * 5)
    res = asyncio.run(safe_send(m, max_attempts=3))
    assert res is None
    assert m.calls == 3
    assert len(no_sleep) == 2              # между 3 попытками — 2 паузы


def test_safe_send_total_wait_cap(no_sleep):
    """Суммарное ожидание не превышает max_total_wait — немедленный отказ."""
    m = FakeMethod([make_retry_after(100), make_retry_after(100)])
    res = asyncio.run(safe_send(m, max_attempts=5, max_total_wait=10.0, cap=200.0))
    assert res is None                     # 100+0.5 > 10 — даже одна пауза невозможна
    assert m.calls == 1
    assert no_sleep == []                  # не спали вообще


def test_safe_send_pause_respects_cap(no_sleep):
    m = FakeMethod([make_retry_after(3600)])
    res = asyncio.run(safe_send(m, max_attempts=2, max_total_wait=90.0, cap=5.0))
    assert res == "ok"
    assert no_sleep == [5.0]               # жёсткий кап для фоновых тиков


def test_safe_send_other_api_error_returns_none(no_sleep):
    class Boom(TelegramBadRequest):
        pass

    m = FakeMethod([TelegramBadRequest(method=object(), message="chat not found")])
    assert asyncio.run(safe_send(m)) is None
    assert m.calls == 1                    # без ретраев на не-flood ошибки


def test_safe_edit_not_modified_is_success():
    class Msg:
        async def edit_text(self, **kw):
            raise TelegramBadRequest(method=object(), message="Telegram API error 400: message is not modified")

    assert asyncio.run(safe_edit(Msg(), "text")) is True


def test_safe_edit_retries_then_succeeds(no_sleep):
    calls = {"n": 0}

    class Msg:
        async def edit_text(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise make_retry_after(2)

    ok = asyncio.run(safe_edit(Msg(), "text"))
    assert ok is True
    assert calls["n"] == 2
    assert no_sleep == [2.5]


def test_safe_edit_fails_after_flood_exhausted(no_sleep):
    class Msg:
        async def edit_text(self, **kw):
            raise make_retry_after(1)

    assert asyncio.run(safe_edit(Msg(), "text", max_attempts=2)) is False


def test_retry_pause_handles_broken_exc():
    class Weird:
        retry_after = "not-a-number"

    assert _retry_pause(Weird(), cap=60.0) == 1.5  # fallback 1.0 + 0.5
