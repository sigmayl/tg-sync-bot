"""持久化任务队列（SQLite）。

每条 /sync 命令对应一条任务记录，状态变化实时落库，进程重启后：
- ``running`` → 重新排队（上次被中断）
- ``queued``  → 重新排队
- ``paused``  → 保持暂停，等用户点「继续」
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    chat_id           INTEGER NOT NULL,
    message_id        INTEGER NOT NULL,
    status_message_id INTEGER,
    urls              TEXT NOT NULL,
    upload_chat       TEXT NOT NULL,
    reply_to          INTEGER,
    state             TEXT NOT NULL,
    detail            TEXT NOT NULL DEFAULT '',
    progress          INTEGER NOT NULL DEFAULT -1,
    force_download    INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_message ON tasks(chat_id, message_id);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
"""


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATES = (TaskState.QUEUED, TaskState.RUNNING, TaskState.PAUSED)
TERMINAL_STATES = (TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED)


@dataclass(slots=True)
class Task:
    id: str
    chat_id: int
    message_id: int
    urls: list[str]
    upload_chat: int | str
    reply_to: int | None
    state: TaskState
    detail: str
    progress: int
    status_message_id: int | None
    created_at: float
    updated_at: float
    force_download: bool = False

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def link_summary(self) -> str:
        if not self.urls:
            return ""
        first = self.urls[0]
        if len(self.urls) == 1:
            return first
        return f"{first} 等 {len(self.urls)} 个链接"


class TaskStore:
    """SQLite 任务表。单线程（事件循环）访问，写入即时提交。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(tasks)")}
        if "force_download" not in columns:
            self._db.execute(
                "ALTER TABLE tasks ADD COLUMN force_download INTEGER NOT NULL DEFAULT 0"
            )
            log.info("任务表已升级：新增 force_download 列")

    # ------------------------------------------------------------------ 读

    def get(self, task_id: str) -> Task | None:
        row = self._db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return _row_to_task(row) if row else None

    def find_by_message(self, chat_id: int, message_id: int) -> Task | None:
        row = self._db.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        return _row_to_task(row) if row else None

    def active(self) -> list[Task]:
        rows = self._db.execute(
            "SELECT * FROM tasks WHERE state IN (?, ?, ?) ORDER BY created_at",
            tuple(state.value for state in ACTIVE_STATES),
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    def queued(self) -> list[Task]:
        rows = self._db.execute(
            "SELECT * FROM tasks WHERE state = ? ORDER BY created_at",
            (TaskState.QUEUED.value,),
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    # ------------------------------------------------------------------ 写

    def add(
        self,
        *,
        chat_id: int,
        message_id: int,
        urls: list[str] | tuple[str, ...],
        upload_chat: int | str,
        reply_to: int | None,
        force_download: bool = False,
    ) -> Task:
        now = time.time()
        task_id = uuid.uuid4().hex[:8]
        self._db.execute(
            "INSERT INTO tasks (id, chat_id, message_id, urls, upload_chat,"
            " reply_to, state, detail, progress, created_at, updated_at,"
            " force_download) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                chat_id,
                message_id,
                json.dumps(list(urls)),
                str(upload_chat),
                reply_to,
                TaskState.QUEUED.value,
                "⏳ 排队中",
                -1,
                now,
                now,
                1 if force_download else 0,
            ),
        )
        self._db.commit()
        task = self.get(task_id)
        assert task is not None
        return task

    def set_state(
        self,
        task_id: str,
        state: TaskState,
        detail: str | None = None,
        progress: int | None = None,
    ) -> None:
        fields = ["state = ?", "updated_at = ?"]
        params: list[object] = [state.value, time.time()]
        if detail is not None:
            fields.append("detail = ?")
            params.append(detail)
        if progress is not None:
            fields.append("progress = ?")
            params.append(progress)
        params.append(task_id)
        self._db.execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", params
        )
        self._db.commit()

    def set_detail(
        self, task_id: str, detail: str, progress: int | None = None
    ) -> None:
        """只更新进度文案；任务已被取消/暂停时不覆盖。"""
        if progress is None:
            self._db.execute(
                "UPDATE tasks SET detail = ?, updated_at = ?"
                " WHERE id = ? AND state = ?",
                (detail, time.time(), task_id, TaskState.RUNNING.value),
            )
        else:
            self._db.execute(
                "UPDATE tasks SET detail = ?, progress = ?, updated_at = ?"
                " WHERE id = ? AND state = ?",
                (detail, progress, time.time(), task_id, TaskState.RUNNING.value),
            )
        self._db.commit()

    def set_status_message(self, task_id: str, message_id: int | None) -> None:
        self._db.execute(
            "UPDATE tasks SET status_message_id = ?, updated_at = ? WHERE id = ?",
            (message_id, time.time(), task_id),
        )
        self._db.commit()

    def requeue_running(self) -> int:
        """启动时把上次中断的 running 任务放回队列。"""
        cursor = self._db.execute(
            "UPDATE tasks SET state = ?, detail = ?, updated_at = ? WHERE state = ?",
            (
                TaskState.QUEUED.value,
                "⏳ 重启后恢复排队",
                time.time(),
                TaskState.RUNNING.value,
            ),
        )
        self._db.commit()
        return cursor.rowcount or 0

    def close(self) -> None:
        self._db.close()


def _row_to_task(row: sqlite3.Row) -> Task:
    upload_chat_raw = row["upload_chat"]
    upload_chat: int | str
    try:
        upload_chat = int(upload_chat_raw)
    except (TypeError, ValueError):
        upload_chat = upload_chat_raw
    return Task(
        id=row["id"],
        chat_id=row["chat_id"],
        message_id=row["message_id"],
        urls=list(json.loads(row["urls"])),
        upload_chat=upload_chat,
        reply_to=row["reply_to"],
        state=TaskState(row["state"]),
        detail=row["detail"],
        progress=row["progress"],
        status_message_id=row["status_message_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        force_download=bool(row["force_download"]),
    )
