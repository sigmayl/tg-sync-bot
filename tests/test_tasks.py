from __future__ import annotations

from pathlib import Path

from tg_sync.tasks import TaskState, TaskStore


def make_store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "nested" / "tasks.db")


def add(store: TaskStore, message_id: int = 1, **kwargs):
    return store.add(
        chat_id=100,
        message_id=message_id,
        urls=kwargs.pop("urls", ["https://x.com/a/status/1"]),
        upload_chat=kwargs.pop("upload_chat", 100),
        reply_to=kwargs.pop("reply_to", message_id),
        **kwargs,
    )


def test_add_and_get(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = add(store)

    assert len(task.id) == 8
    assert task.state == TaskState.QUEUED
    assert task.urls == ["https://x.com/a/status/1"]
    assert task.upload_chat == 100
    assert task.reply_to == 1
    assert store.get(task.id) == task


def test_upload_chat_username_roundtrip(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = add(store, upload_chat="@channel")
    assert store.get(task.id).upload_chat == "@channel"


def test_find_by_message_and_unique(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = add(store, message_id=5)
    assert store.find_by_message(100, 5).id == task.id
    assert store.find_by_message(100, 6) is None


def test_set_state_and_detail(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = add(store)

    store.set_state(task.id, TaskState.RUNNING, "🔍 开始")
    store.set_detail(task.id, "⬇️ 下载中")
    assert store.get(task.id).detail == "⬇️ 下载中"

    store.set_detail(task.id, "⬆️ 上传中：45%", progress=45)
    updated = store.get(task.id)
    assert updated.state == TaskState.RUNNING
    assert updated.detail == "⬆️ 上传中：45%"
    assert updated.progress == 45


def test_set_detail_ignored_when_not_running(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = add(store)
    store.set_state(task.id, TaskState.PAUSED, "⏸ 已暂停")

    store.set_detail(task.id, "⬆️ 上传中：90%", progress=90)

    assert store.get(task.id).detail == "⏸ 已暂停"


def test_active_filters_terminal_states(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    queued = add(store, message_id=1)
    running = add(store, message_id=2)
    paused = add(store, message_id=3)
    done = add(store, message_id=4)
    store.set_state(running.id, TaskState.RUNNING)
    store.set_state(paused.id, TaskState.PAUSED)
    store.set_state(done.id, TaskState.DONE)

    assert {t.id for t in store.active()} == {queued.id, running.id, paused.id}


def test_requeue_running(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    running = add(store, message_id=1)
    paused = add(store, message_id=2)
    store.set_state(running.id, TaskState.RUNNING)
    store.set_state(paused.id, TaskState.PAUSED)

    assert store.requeue_running() == 1
    assert store.get(running.id).state == TaskState.QUEUED
    assert store.get(paused.id).state == TaskState.PAUSED
    assert [t.id for t in store.queued()] == [running.id]


def test_status_message_id_persists(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = add(store)
    store.set_status_message(task.id, 777)
    store.close()

    reopened = TaskStore(tmp_path / "nested" / "tasks.db")
    assert reopened.get(task.id).status_message_id == 777


def test_link_summary(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    one = add(store, message_id=1)
    two = add(store, message_id=2, urls=["https://a", "https://b"])
    assert one.link_summary == "https://x.com/a/status/1"
    assert two.link_summary == "https://a 等 2 个链接"


def test_force_download_roundtrip(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    task = add(store)
    forced = add(store, message_id=2)
    store.set_state(task.id, TaskState.QUEUED)
    assert store.get(task.id).force_download is False
    assert store.get(forced.id).force_download is False

    forced_task = store.add(
        chat_id=100,
        message_id=3,
        urls=["https://t.me/a/1"],
        upload_chat=100,
        reply_to=3,
        force_download=True,
    )
    assert store.get(forced_task.id).force_download is True


def test_migrates_old_schema(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL, status_message_id INTEGER,
            urls TEXT NOT NULL, upload_chat TEXT NOT NULL, reply_to INTEGER,
            state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
            progress INTEGER NOT NULL DEFAULT -1,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        """
    )
    db.execute(
        "INSERT INTO tasks VALUES ('old12345', 100, 1, NULL, ?, '100', 1,"
        " 'done', 'old', -1, 1.0, 1.0)",
        ('["https://a"]',),
    )
    db.commit()
    db.close()

    store = TaskStore(path)
    task = store.get("old12345")
    assert task is not None
    assert task.force_download is False
    assert task.state == TaskState.DONE
