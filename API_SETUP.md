# Настройка API smmway.ru

## Проблема с 404 ошибкой

Если вы получаете ошибку 404 при синхронизации каталога, это означает, что эндпоинт API неправильный.

## Решение

1. **Проверьте документацию API**: https://smmway.ru/developer
2. **Убедитесь в правильности API ключа**
3. **Проверьте правильный базовый URL API**

## Возможные варианты URL

Попробуйте изменить `api_url` в `main.py`:

```python
smmway_api = SMMWayAPI(
    api_key="ваш_ключ",
    api_url="https://smmway.ru/api"  # Без /v2
)
# или
smmway_api = SMMWayAPI(
    api_key="ваш_ключ",
    api_url="https://api.smmway.ru/v2"  # Другой домен
)
```

## Альтернатива: Ручное заполнение каталога

Если API не работает, вы можете заполнить каталог вручную через Python:

```python
from functions.db.db import Database

async def add_manual_catalog():
    db = Database()
    await db.init()
    
    # Добавляем приложение
    app = await db.add_app("Telegram")
    
    # Добавляем услугу
    service = await db.add_service(app.id, "Подписчики", service_api_id=123)
    
    # Добавляем пак с наценкой 250%
    price = await db.add_price(
        service.id,
        "100-1000 подписчиков",
        100.0,  # Цена покупки
        250.0   # Наценка в процентах
    )
```

## Проверка подключения

При запуске бота проверьте логи:
- Если видите "✅ Подключение к API успешно!" - всё работает
- Если видите "❌ Эндпоинт не найден (404)" - нужно исправить URL

