from pathlib import Path

from cr_agent.core.storage import TaskStore
from cr_agent.models import TaskRecord, TaskStatus, TriggerRequest


def test_task_store_save_uses_atomic_replace(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks")
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    record = TaskRecord(task_id="task1", status=TaskStatus.queued, request=request)

    store.save(record)

    assert (tmp_path / "tasks" / "task1.json").exists()
    assert not (tmp_path / "tasks" / "task1.json.tmp").exists()
    assert list((tmp_path / "tasks").glob("*.tmp")) == []


def test_task_store_get_reads_saved_record(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks")
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    record = TaskRecord(task_id="task1", status=TaskStatus.queued, request=request)
    store.save(record)

    loaded = store.get("task1")

    assert loaded is not None
    assert loaded.task_id == "task1"
    assert loaded.request.app_name == "demo"
