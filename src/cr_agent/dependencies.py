from functools import lru_cache

from cr_agent.config import get_settings
from cr_agent.core.service import TaskService


@lru_cache(maxsize=1)
def get_task_service() -> TaskService:
    settings = get_settings()
    service = TaskService(settings)
    service.start()
    return service
