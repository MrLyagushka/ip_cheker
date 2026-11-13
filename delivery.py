"""Delivery helpers: flood-safe отправка/редактирование сообщений (Б25).

Настоящая причина «сканирование останавливается» по логам: пинги завершались
успешно, но финальная `await message.answer(final_message)` ловила
необработанный `TelegramRetryAfter` — в группе ~300 юзеров поток «текст +
документ + алерты мониторинга» пробивает лимит Telegram ~1 msg/sec на чат.
Исключение глоталось внешним `except Exception`, и пользователь видел только
«Сканирование завершилось с ошибкой».

Правила доставки (https://core.telegram.org/bots/faq#my-bot-is-hitting-limits):
* `safe_send` / `safe_edit` — ретрай строго по `e.retry_after` (плюс небольшой
  запас), с ограничением суммарного ожидания (`max_total_wait`);
* для фоновых тиков пауза дополнительно каппится (`cap`) — автоотчёт не должен
  блокировать цикл на десятки минут;
* между массовыми рассылками — pacing `SEND_PACING` (1.05 сек > лимита
  ~1 msg/sec в один чат);
* редактирование «message is not modified» (400) — не ошибка доставки:
  текст уже показан, считаем успехом.

Функции возвращают результат вызова (Message/True) либо None, если все
попытки исчерпаны — вызывающая сторона решает, нужен ли fallback
(например, дублировать сообщение новым send вместо edit).
"""

import asyncio
import logging

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter

logger = logging.getLogger(__name__)

# Лимит Telegram — ~1 сообщение в секунду в один чат: шаг рассылки с запасом.
SEND_PACING = 1.05
# Разовая пауза по RetryAfter не должна превращаться в вечное ожидание.
DEFAULT_MAX_PAUSE = 60.0


def _retry_pause(exc: TelegramRetryAfter, cap: float) -> float:
    """Пауза из e.retry_after (+0.5 сек запас) с жёстким верхним капом."""
    try:
        raw = float(getattr(exc, "retry_after", 1) or 1)
    except (TypeError, ValueError):
        raw = 1.0
    return min(raw + 0.5, cap)


async def safe_send(method, *args, max_attempts: int = 3,
                    max_total_wait: float = 90.0, cap: float = DEFAULT_MAX_PAUSE,
                    **kwargs):
    """Отправка через бинд метода (`bot.send_message` / `message.answer` / …).

    Возвращает результат успешного вызова или None после исчерпания попыток.
    RetryAfter → sleep(min(e.retry_after + 0.5, cap)) → повтор, пока суммарное
    ожидание не превысит max_total_wait. Прочие ошибки Telegram логируются и
    приводят к отказу (None) — решение о fallback принимает вызывающий код.
    """
    waited = 0.0
    for attempt in range(1, max_attempts + 1):
        try:
            return await method(*args, **kwargs)
        except TelegramRetryAfter as e:
            pause = _retry_pause(e, cap)
            if attempt >= max_attempts or waited + pause > max_total_wait:
                logger.warning("Flood limit (%s), попытки исчерпаны: %r",
                               getattr(method, "__name__", method), e)
                return None
            waited += pause
            logger.info("RetryAfter %s сек (попытка %d/%d)", pause, attempt, max_attempts)
            await asyncio.sleep(pause)
        except TelegramAPIError as e:
            logger.warning("Не удалось отправить (%s): %r",
                           getattr(method, "__name__", method), e)
            return None
    return None


async def safe_edit(target, text: str, reply_markup=None, *, parse_mode: str = "HTML",
                    max_attempts: int = 3, max_total_wait: float = 90.0,
                    cap: float = DEFAULT_MAX_PAUSE) -> bool:
    """Редактирование сообщения объекта `target` (Message) с ретраем по flood.

    Совместим по смыслу со старым `_safe_edit` (Б17): True — текст показан
    (включая случай «message is not modified»), False — редактирование
    недоступно, вызывающему коду пора использовать fallback-отправку.
    """
    waited = 0.0
    kwargs = {"text": text, "parse_mode": parse_mode}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    for attempt in range(1, max_attempts + 1):
        try:
            await target.edit_text(**kwargs)
            return True
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return True          # цель уже достигнута — это не провал
            logger.warning("Edit отклонён Telegram: %r", e)
            return False
        except TelegramRetryAfter as e:
            pause = _retry_pause(e, cap)
            if attempt >= max_attempts or waited + pause > max_total_wait:
                logger.warning("Flood limit при edit, попытки исчерпаны: %r", e)
                return False
            waited += pause
            logger.info("RetryAfter %s сек при edit (попытка %d/%d)",
                        pause, attempt, max_attempts)
            await asyncio.sleep(pause)
        except TelegramAPIError as e:
            logger.warning("Edit недоступен: %r", e)
            return False
    return False


async def pace(seconds: float = SEND_PACING) -> None:
    """Пауза между массовыми рассылками (1 msg/sec на чат + запас)."""
    await asyncio.sleep(seconds)
