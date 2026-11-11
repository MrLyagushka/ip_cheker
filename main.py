import asyncio
from config import dp, bot, init_files
from handlers import start, scan, tree, admin, monitor
from handlers.middlewares import UserRegistrationMiddleware
from handlers.monitor import hourly_monitor_loop


async def monitor_supervisor():
    """Supervisor фоновой задачи мониторинга (Б10/Б22).

    Если `hourly_monitor_loop` по какой-то причине завершится с ошибкой
    (например, при future-регрессии), задача будет перезапущена через 60 сек.
    CancelledError пробрасывается — корректная остановка при shutdown.
    """
    while True:
        try:
            await hourly_monitor_loop()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Монитор упал: {e!r}. Перезапуск через 60 секунд...")
            await asyncio.sleep(60)


async def main():
    init_files()
    
    # Этап 5 (Б7): outer-middleware регистрирует пользователя в users.json
    # на ЛЮБОЙ входящий update (сообщение/колбэк), а не только через /start.
    dp.update.outer_middleware(UserRegistrationMiddleware())
    
    # Подключаем роутеры
    dp.include_router(start.router)
    dp.include_router(scan.router)
    dp.include_router(tree.router)
    dp.include_router(admin.router)
    dp.include_router(monitor.router)
    
    # Запуск фоновой задачи ежечасного мониторинга под supervisor'ом
    asyncio.create_task(monitor_supervisor())
    
    print("Бот успешно запущен и ожидает команды...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
