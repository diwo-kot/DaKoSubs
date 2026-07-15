import os
import shutil
import asyncio
from datetime import datetime, time, timedelta

# Импортируем основную функцию запуска бота из main.py
from main import main as run_bot

# --- Настройки ---
# Директории
DATA_DIR = "data"
BACKUPS_DIR = "backups"
DB_FILENAME = "bot.db"

# Время для ежедневного бэкапа (формат ЧЧ:ММ)
BACKUP_TIME = "03:00"
# -----------------

DB_PATH = os.path.join(DATA_DIR, DB_FILENAME)

def setup_directories():
    """Создает необходимые директории, если они не существуют."""
    print("Проверка и создание директорий...")
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    print("✅ Директории готовы.")

def create_backup():
    """Создает резервную копию базы данных."""
    if not os.path.exists(DB_PATH):
        print(f"⚠️ Файл базы данных {DB_PATH} не найден. Бэкап пропущен.")
        return

    try:
        # Формируем имя файла для бэкапа
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_filename = f"backup_{timestamp}.db"
        backup_path = os.path.join(BACKUPS_DIR, backup_filename)

        # Копируем файл
        shutil.copy2(DB_PATH, backup_path)
        print(f"✅ Успешный бэкап базы данных: {backup_path}")
    except Exception as e:
        print(f"❌ Ошибка при создании бэкапа: {e}")

async def backup_scheduler():
    """Асинхронный планировщик для создания бэкапов."""
    print(f"Планировщик бэкапов запущен. Бэкапы будут создаваться каждый день в {BACKUP_TIME}.")
    
    while True:
        now = datetime.now()
        
        # Парсим время из настроек
        backup_hour, backup_minute = map(int, BACKUP_TIME.split(':'))
        
        # Устанавливаем время следующего бэкапа на сегодня
        next_backup_time = now.replace(hour=backup_hour, minute=backup_minute, second=0, microsecond=0)
        
        # Если это время уже прошло сегодня, планируем на завтра
        if now >= next_backup_time:
            next_backup_time += timedelta(days=1)
            
        # Считаем, сколько секунд спать до следующего бэкапа
        sleep_seconds = (next_backup_time - now).total_seconds()
        
        print(f"Следующий бэкап через: {timedelta(seconds=int(sleep_seconds))}")
        
        await asyncio.sleep(sleep_seconds)
        
        # Время пришло, делаем бэкап
        create_backup()
        
        # Небольшая задержка, чтобы избежать повторного запуска в ту же секунду
        await asyncio.sleep(60)

async def start_app():
    """Основная точка входа: настраивает окружение и запускает бота."""
    # 1. Устанавливаем рабочую директорию в корень проекта
    # Это гарантирует, что все относительные пути (например, к БД) будут работать корректно
    project_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_root)
    print(f"Рабочая директория установлена: {os.getcwd()}")

    # 2. Создаем папки
    setup_directories()
    
    # 3. Запускаем планировщик бэкапов в фоновой задаче
    backup_task = asyncio.create_task(backup_scheduler())
    
    # 4. Запускаем основную логику бота
    await run_bot()

if __name__ == "__main__":
    try:
        asyncio.run(start_app())
    except (KeyboardInterrupt, SystemExit):
        print("\nВыход из приложения.")