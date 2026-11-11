import asyncio
from config import dp, bot, init_files
from handlers import start, scan, tree, admin, monitor
from handlers.monitor import hourly_monitor_loop

async def main():
    init_files()
    
    # Подключаем роутеры
    dp.include_router(start.router)
    dp.include_router(scan.router)
    dp.include_router(tree.router)
    dp.include_router(admin.router)
    dp.include_router(monitor.router)
    
    # Запуск фоновой задачи ежечасного мониторинга
    asyncio.create_task(hourly_monitor_loop())
    
    print("Бот успешно запущен и ожидает команды...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
