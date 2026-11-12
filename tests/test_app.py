from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from tg_sync import app as app_module
from tg_sync.app import SyncApp
from tg_sync.config import Config
from tg_sync.downloaders.base import DownloadError
from tg_sync.models import MediaItem
from tg_sync.tasks import Task, TaskState
from tg_sync.uploader import Uploader

URL = "https://x.com/NASA/status/123"


class FakeStatus:
    def __init__(self, message_id: int, text: str, buttons=None) -> None:
        self.id = message_id
        self.text = text
        self.buttons = buttons
        self.edits: list[str] = []
        self.deleted = False

    async def edit(self, text, buttons=None):
        self.text = text
        self.buttons = buttons
        self.edits.append(text)

    async def delete(self) -> None:
        self.deleted = True


class FakeSent:
    pass


class FakeClient:
    def __init__(self) -> None:
        self.messages: dict[int, FakeStatus] = {}
        self.sent: list[tuple[object, str, int | None, FakeStatus]] = []
        self.files: list[tuple[object, object, dict]] = []
        self._next_id = 100

    async def send_message(self, entity, text, reply_to=None, buttons=None):
        self._next_id += 1
        status = FakeStatus(self._next_id, text, buttons)
        self.messages[status.id] = status
        self.sent.append((entity, text, reply_to, status))
        return status

    async def get_messages(self, entity, ids=None, **kwargs):
        if isinstance(ids, int):
            return self.messages.get(ids)
        return [self.messages.get(i) for i in ids or []]

    async def send_file(self, entity, file, **kwargs):
        self.files.append((entity, file, kwargs))
        return FakeSent()


class FakeEvent:
    def __init__(
        self,
        text: str,
        *,
        sender_id: int = 7,
        chat_id: int = 100,
        message_id: int = 5,
    ) -> None:
        self.raw_text = text
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.message = SimpleNamespace(id=message_id)
        self.replies: list[tuple[str, object]] = []

    async def reply(self, text, buttons=None):
        self.replies.append((text, buttons))


class FakeCallback:
    def __init__(self, data: str, *, sender_id: int = 7) -> None:
        self.data = data.encode()
        self.sender_id = sender_id
        self.answers: list[tuple[str | None, bool]] = []
        self.edits: list[tuple[str, object]] = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def edit(self, text, buttons=None):
        self.edits.append((text, buttons))


class FakeDownloader:
    name = "fake"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def download(self, link, workdir: Path) -> list[MediaItem]:
        if self.error is not None:
            raise self.error
        path = workdir / "media.jpg"
        path.write_bytes(b"x" * 10)
        return [MediaItem(path, source_url=link.url, title="标题")]


class FakeTgDownloader:
    name = "telegram"

    def __init__(self, count: int | None = None, error: Exception | None = None) -> None:
        self.count = count
        self.error = error
        self.transfers: list[tuple[str, object, object]] = []
        self.downloads: list[str] = []

    async def transfer(self, link, dest_chat, reply_to=None):
        self.transfers.append((link.url, dest_chat, reply_to))
        if self.error is not None:
            raise self.error
        return self.count

    async def download(self, link, workdir: Path) -> list[MediaItem]:
        self.downloads.append(link.url)
        path = workdir / "tg.jpg"
        path.write_bytes(b"x" * 10)
        return [MediaItem(path, source_url=link.url, title="tg")]


class SlowDownloader:
    name = "slow"

    async def download(self, link, workdir: Path) -> list[MediaItem]:
        await asyncio.sleep(5)
        return []


def make_app(tmp_path: Path, monkeypatch, **config_kwargs) -> tuple[SyncApp, FakeClient]:
    monkeypatch.setattr(app_module, "TelegramClient", lambda *a, **k: FakeClient())
    config_kwargs.setdefault("allowed_users", frozenset({7}))
    config = Config(
        bot_token="1:test",
        api_id=1,
        api_hash="hash",
        session=str(tmp_path / "session"),
        download_dir=tmp_path / "downloads",
        task_db=tmp_path / "state" / "tasks.db",
        max_file_size=1024 * 1024,
        **config_kwargs,
    )
    config.ensure_dirs()
    app = SyncApp(config)
    app.uploader = Uploader(app.client, max_file_size=config.max_file_size)
    return app, app.client


async def start_task(app: SyncApp, message_id: int = 5) -> Task:
    event = FakeEvent(f"/sync {URL}", message_id=message_id)
    await app._on_new_message(event)
    task = app.store.find_by_message(100, message_id)
    assert task is not None
    return task


def button_data(buttons) -> list[str]:
    out: list[str] = []
    for row in buttons or []:
        for button in row:
            data = getattr(getattr(button, "type", None), "data", None)
            if data is None:
                data = getattr(getattr(button, "button", None), "data", None)
            out.append(data.decode() if isinstance(data, bytes) else str(data))
    return out


# --------------------------------------------------------------------- 命令入口


async def test_sync_creates_persistent_task_with_buttons(
    tmp_path: Path, monkeypatch
) -> None:
    app, client = make_app(tmp_path, monkeypatch)

    task = await start_task(app)

    assert task.state == TaskState.QUEUED
    assert task.urls == [URL]
    assert app.queue.get_nowait() == task.id
    status = client.messages[task.status_message_id]
    assert f"#{task.id}" not in status.text  # 状态文本用 🆔
    assert f"🆔 {task.id}" in status.text
    assert button_data(status.buttons) == [f"pause:{task.id}", f"cancel:{task.id}"]


async def test_duplicate_message_is_ignored(tmp_path: Path, monkeypatch) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    await start_task(app)
    await start_task(app)
    assert len(app.store.active()) == 1


async def test_unauthorized_user_is_rejected(tmp_path: Path, monkeypatch) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    event = FakeEvent(f"/sync {URL}", sender_id=999)

    await app._on_new_message(event)

    assert "user id 是 999" in event.replies[0][0]
    assert app.store.active() == []


async def test_help_and_id(tmp_path: Path, monkeypatch) -> None:
    app, _client = make_app(tmp_path, monkeypatch)

    help_event = FakeEvent("/help")
    await app._on_new_message(help_event)
    assert "可用命令" in help_event.replies[0][0]

    id_event = FakeEvent("/id")
    await app._on_new_message(id_event)
    assert "chat_id: 100" in id_event.replies[0][0]


# --------------------------------------------------------------------- 按钮


async def test_pause_and_resume_queued_task(tmp_path: Path, monkeypatch) -> None:
    app, client = make_app(tmp_path, monkeypatch)
    task = await start_task(app)

    pause = FakeCallback(f"pause:{task.id}")
    await app._on_callback(pause)

    assert pause.answers == [("已暂停", False)]
    paused = app.store.get(task.id)
    assert paused.state == TaskState.PAUSED
    status = client.messages[task.status_message_id]
    assert "已暂停" in status.text
    assert button_data(status.buttons) == [f"resume:{task.id}", f"cancel:{task.id}"]

    resume = FakeCallback(f"resume:{task.id}")
    await app._on_callback(resume)

    assert resume.answers == [("已继续", False)]
    assert app.store.get(task.id).state == TaskState.QUEUED


async def test_cancel_running_task(tmp_path: Path, monkeypatch) -> None:
    app, client = make_app(tmp_path, monkeypatch)
    app.x_downloader = SlowDownloader()
    task = await start_task(app)
    app.queue.get_nowait()

    runner = asyncio.create_task(app._run_job(task.id))
    await asyncio.sleep(0.05)

    cancel = FakeCallback(f"cancel:{task.id}")
    await app._on_callback(cancel)
    await asyncio.gather(runner, return_exceptions=True)

    assert cancel.answers == [("已取消", False)]
    assert app.store.get(task.id).state == TaskState.CANCELLED
    status = client.messages[task.status_message_id]
    assert "已取消" in status.text
    assert button_data(status.buttons) == [f"retry:{task.id}"]
    assert list((tmp_path / "downloads").iterdir()) == []


async def test_pause_running_task_keeps_it_resumable(
    tmp_path: Path, monkeypatch
) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    app.x_downloader = SlowDownloader()
    task = await start_task(app)
    app.queue.get_nowait()

    runner = asyncio.create_task(app._run_job(task.id))
    await asyncio.sleep(0.05)

    pause = FakeCallback(f"pause:{task.id}")
    await app._on_callback(pause)
    await asyncio.gather(runner, return_exceptions=True)

    assert app.store.get(task.id).state == TaskState.PAUSED


async def test_callback_from_stranger_is_rejected(tmp_path: Path, monkeypatch) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    task = await start_task(app)

    callback = FakeCallback(f"cancel:{task.id}", sender_id=999)
    await app._on_callback(callback)

    assert callback.answers == [("没有权限", True)]
    assert app.store.get(task.id).state == TaskState.QUEUED


# --------------------------------------------------------------------- 任务列表


async def test_task_list_shows_only_active(tmp_path: Path, monkeypatch) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    running = await start_task(app, message_id=5)
    paused = await start_task(app, message_id=6)
    done = await start_task(app, message_id=7)
    app.store.set_state(running.id, TaskState.RUNNING, "⬇️ 下载中")
    app.store.set_state(paused.id, TaskState.PAUSED, "⏸ 已暂停")
    app.store.set_state(done.id, TaskState.DONE, "✅ 完成")

    event = FakeEvent("/tasks")
    await app._on_new_message(event)

    text, buttons = event.replies[0]
    assert "进行中的任务（2）" in text
    assert running.id in text
    assert paused.id in text
    assert done.id not in text
    assert button_data(buttons) == [
        f"pause:{running.id}",
        f"cancel:{running.id}",
        f"resume:{paused.id}",
        f"cancel:{paused.id}",
        "list",
    ]


async def test_task_list_empty(tmp_path: Path, monkeypatch) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    event = FakeEvent("/tasks")
    await app._on_new_message(event)
    assert event.replies[0][0] == "当前没有进行中的任务"
    assert event.replies[0][1] is None


async def test_task_list_refresh_button(tmp_path: Path, monkeypatch) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    await start_task(app)

    callback = FakeCallback("list")
    await app._on_callback(callback)

    assert callback.answers == [(None, False)]
    assert "进行中的任务（1）" in callback.edits[0][0]


# --------------------------------------------------------------------- 处理流程


async def test_success_flow(tmp_path: Path, monkeypatch) -> None:
    app, client = make_app(tmp_path, monkeypatch)
    app.x_downloader = FakeDownloader()
    task = await start_task(app)
    app.queue.get_nowait()

    await app._run_job(task.id)

    assert len(client.files) == 1
    entity, file_arg, kwargs = client.files[0]
    assert entity == 100
    assert kwargs["reply_to"] == 5
    assert kwargs["caption"] == "标题\nhttps://x.com/NASA/status/123"
    assert str(file_arg).endswith("media.jpg")
    assert client.messages[task.status_message_id].deleted is True
    assert app.store.get(task.id).state == TaskState.DONE
    assert list((tmp_path / "downloads").iterdir()) == []


async def test_download_failure_is_reported(tmp_path: Path, monkeypatch) -> None:
    app, client = make_app(tmp_path, monkeypatch)
    app.x_downloader = FakeDownloader(error=DownloadError("网络炸了"))
    task = await start_task(app)
    app.queue.get_nowait()

    await app._run_job(task.id)

    status = client.messages[task.status_message_id]
    assert "❌" in status.text
    assert "网络炸了" in status.text
    assert button_data(status.buttons) == [f"retry:{task.id}"]
    assert app.store.get(task.id).state == TaskState.FAILED
    assert client.files == []


async def test_partial_failure_keeps_summary(tmp_path: Path, monkeypatch) -> None:
    app, client = make_app(tmp_path, monkeypatch)
    app.x_downloader = FakeDownloader()
    event = FakeEvent(f"/sync https://example.com/nope {URL}")
    await app._on_new_message(event)
    task = app.store.find_by_message(100, 5)
    app.queue.get_nowait()

    await app._run_job(task.id)

    status = client.messages[task.status_message_id]
    assert "已发送 1 个文件" in status.text
    assert "example.com" in status.text
    assert app.store.get(task.id).state == TaskState.DONE


async def test_status_messages_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    app, client = make_app(tmp_path, monkeypatch, status_messages=False)
    app.x_downloader = FakeDownloader()
    task = await start_task(app)
    app.queue.get_nowait()

    await app._run_job(task.id)

    assert client.sent == []
    assert len(client.files) == 1
    assert app.store.get(task.id).state == TaskState.DONE


async def test_keep_files(tmp_path: Path, monkeypatch) -> None:
    app, _client = make_app(tmp_path, monkeypatch, keep_files=True)
    app.x_downloader = FakeDownloader()
    task = await start_task(app)
    app.queue.get_nowait()

    await app._run_job(task.id)

    kept = list((tmp_path / "downloads").iterdir())
    assert len(kept) == 1
    assert (kept[0] / "media.jpg").exists()


async def test_upload_goes_to_target_chat(tmp_path: Path, monkeypatch) -> None:
    app, client = make_app(tmp_path, monkeypatch, target_chat=-100999)
    app.x_downloader = FakeDownloader()
    task = await start_task(app)
    app.queue.get_nowait()

    await app._run_job(task.id)

    entity, _file_arg, kwargs = client.files[0]
    assert entity == -100999
    assert kwargs["reply_to"] is None


async def test_job_timeout_marks_failed(tmp_path: Path, monkeypatch) -> None:
    app, client = make_app(tmp_path, monkeypatch, job_timeout=0.05)
    app.x_downloader = SlowDownloader()
    task = await start_task(app)
    app.queue.get_nowait()

    await asyncio.wait_for(app._run_job(task.id), timeout=5)

    status = client.messages[task.status_message_id]
    assert "超时" in status.text
    assert app.store.get(task.id).state == TaskState.FAILED


# --------------------------------------------------------------------- 重启恢复


async def test_restart_requeues_unfinished_tasks(tmp_path: Path, monkeypatch) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    queued = await start_task(app, message_id=5)
    running = await start_task(app, message_id=6)
    paused = await start_task(app, message_id=7)
    done = await start_task(app, message_id=8)
    app.store.set_state(running.id, TaskState.RUNNING, "⬇️ 下载中")
    app.store.set_state(paused.id, TaskState.PAUSED, "⏸ 已暂停")
    app.store.set_state(done.id, TaskState.DONE, "✅ 完成")
    app.store.close()

    restarted, _client2 = make_app(tmp_path, monkeypatch)
    restored = restarted._restore_tasks()

    assert restored == 2
    assert restarted.store.get(queued.id).state == TaskState.QUEUED
    assert restarted.store.get(running.id).state == TaskState.QUEUED
    assert restarted.store.get(paused.id).state == TaskState.PAUSED
    assert restarted.store.get(done.id).state == TaskState.DONE

    queued_ids = set()
    while not restarted.queue.empty():
        queued_ids.add(restarted.queue.get_nowait())
    assert queued_ids == {queued.id, running.id}


async def test_drain_keeps_queued_tasks_for_restart(
    tmp_path: Path, monkeypatch
) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    task = await start_task(app)

    await app._drain(0.1)

    assert app.store.get(task.id).state == TaskState.QUEUED


# --------------------------------------------------------------------- 重试


async def test_retry_button_requeues_failed_task(tmp_path: Path, monkeypatch) -> None:
    app, client = make_app(tmp_path, monkeypatch)
    app.x_downloader = FakeDownloader(error=DownloadError("boom"))
    task = await start_task(app)
    app.queue.get_nowait()
    await app._run_job(task.id)
    assert app.store.get(task.id).state == TaskState.FAILED

    retry = FakeCallback(f"retry:{task.id}")
    await app._on_callback(retry)

    assert retry.answers == [("已重新排队", False)]
    retried = app.store.get(task.id)
    assert retried.state == TaskState.QUEUED
    assert retried.progress == -1
    assert "重试" in retried.detail
    assert app.queue.get_nowait() == task.id
    status = client.messages[task.status_message_id]
    assert "排队中" in status.text
    assert button_data(status.buttons) == [f"pause:{task.id}", f"cancel:{task.id}"]


async def test_retry_command(tmp_path: Path, monkeypatch) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    app.x_downloader = FakeDownloader(error=DownloadError("boom"))
    task = await start_task(app)
    app.queue.get_nowait()
    await app._run_job(task.id)

    event = FakeEvent(f"/retry {task.id}")
    await app._on_new_message(event)

    assert "已重新排队" in event.replies[0][0]
    assert app.store.get(task.id).state == TaskState.QUEUED


async def test_retry_command_unknown_task(tmp_path: Path, monkeypatch) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    event = FakeEvent("/retry deadbeef")
    await app._on_new_message(event)
    assert "找不到任务" in event.replies[0][0]


async def test_retry_command_without_id_shows_usage(
    tmp_path: Path, monkeypatch
) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    event = FakeEvent("/retry")
    await app._on_new_message(event)
    assert "用法" in event.replies[0][0]


async def test_retry_running_task_is_rejected(tmp_path: Path, monkeypatch) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    app.x_downloader = SlowDownloader()
    task = await start_task(app)
    app.queue.get_nowait()
    runner = asyncio.create_task(app._run_job(task.id))
    await asyncio.sleep(0.05)

    retry = FakeCallback(f"retry:{task.id}")
    await app._on_callback(retry)

    assert retry.answers == [("任务还在进行中", False)]
    assert app.store.get(task.id).state == TaskState.RUNNING

    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)


async def test_cleanup_stale_download_dirs(tmp_path: Path, monkeypatch) -> None:
    app, _client = make_app(tmp_path, monkeypatch)
    stale = tmp_path / "downloads" / "task-deadbeef-abc"
    stale.mkdir(parents=True)
    (stale / "partial.mp4").write_bytes(b"x")
    keep = tmp_path / "downloads" / "manual"
    keep.mkdir()

    app._cleanup_stale_dirs()

    assert not stale.exists()
    assert keep.exists()


# --------------------------------------------------------------------- t.me 转发


async def test_telegram_link_is_forwarded_not_downloaded(
    tmp_path: Path, monkeypatch
) -> None:
    app, client = make_app(tmp_path, monkeypatch)
    tg = FakeTgDownloader(count=4)
    app.tg_downloader = tg
    event = FakeEvent("/sync https://t.me/somechannel/123")
    await app._on_new_message(event)
    task = app.store.find_by_message(100, 5)
    app.queue.get_nowait()

    await app._run_job(task.id)

    assert tg.transfers == [("https://t.me/somechannel/123", 100, 5)]
    assert tg.downloads == []
    assert client.files == []
    assert app.store.get(task.id).state == TaskState.DONE
    assert app.store.get(task.id).detail == "✅ 完成：4 个文件"


async def test_telegram_falls_back_to_download_when_forward_fails(
    tmp_path: Path, monkeypatch
) -> None:
    app, client = make_app(tmp_path, monkeypatch)
    tg = FakeTgDownloader(count=None)
    app.tg_downloader = tg
    event = FakeEvent("/sync https://t.me/somechannel/123")
    await app._on_new_message(event)
    task = app.store.find_by_message(100, 5)
    app.queue.get_nowait()

    await app._run_job(task.id)

    assert tg.downloads == ["https://t.me/somechannel/123"]
    assert len(client.files) == 1
    assert app.store.get(task.id).state == TaskState.DONE


async def test_force_download_skips_forwarding(tmp_path: Path, monkeypatch) -> None:
    app, client = make_app(tmp_path, monkeypatch)
    tg = FakeTgDownloader(count=4)
    app.tg_downloader = tg
    event = FakeEvent("/sync --download https://t.me/somechannel/123")
    await app._on_new_message(event)
    task = app.store.find_by_message(100, 5)
    app.queue.get_nowait()

    await app._run_job(task.id)

    assert tg.transfers == []
    assert tg.downloads == ["https://t.me/somechannel/123"]
    assert len(client.files) == 1
    assert app.store.get(task.id).state == TaskState.DONE
