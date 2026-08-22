import time
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from config import RATE_LIMIT_SECONDS


class ThrottlingMiddleware(BaseMiddleware):
    """
    Middleware для ограничения частоты запросов (Rate Limiting / Anti-Spam).
    Защищает бота и внешние API от флуда и перегрузок.
    """

    def __init__(self, limit: float = RATE_LIMIT_SECONDS):
        super().__init__()
        self.rate_limit = limit
        self.user_timestamps: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Извлекаем пользователя из контекста события
        user: User | None = data.get("event_from_user")

        if user:
            user_id = user.id
            current_time = time.time()
            last_time = self.user_timestamps.get(user_id, 0.0)

            # Проверяем интервал между запросами
            if current_time - last_time < self.rate_limit:
                # Если пользователь шлёт запросы слишком часто — игнорируем их
                return None

            # Обновляем время последнего успешного запроса
            self.user_timestamps[user_id] = current_time

            # Очищаем старые записи (защита от утечки памяти при большом числе пользователей)
            if len(self.user_timestamps) > 10000:
                threshold = current_time - (self.rate_limit * 2)
                self.user_timestamps = {
                    uid: ts for uid, ts in self.user_timestamps.items() if ts > threshold
                }

        return await handler(event, data)