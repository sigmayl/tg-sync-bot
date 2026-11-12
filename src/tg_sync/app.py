"""应用主体：Bot 收到 /sync → 持久化任务 → 下载 → 回复上传。

任务状态存在 SQLite 里，重启后未完成的任务会继续；状态消息带「暂停 / 取消」按钮。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events
from telethon.tl.custom import Button

from .commands import CommandError, command_name, parse_command
from .config import PREMIUM_MAX_FILE_SIZE, Config
from .downloaders import DownloadError, TelegramDownloader, XDownloader
from .links import Link, LinkError, LinkKind, parse_link
from .models import MediaItem
from .tasks import TERMINAL_STATES, Task, TaskState, TaskStore
from .uploader import Uploader, UploadError

log = logging.getLogger(__name__)

MIB = 1024 * 1024
MAX_LISTED_TASKS = 10

HELP_TEXT = (
    "可用命令：\n"
    "/sync [--download] <链接> … — 下载/转发链接里的媒体（最多 10 个）\n"
    "/tasks — 查看进行中的任务（可暂停/取消/继续）\n"
    "/retry <任务ID> — 重新执行失败/取消的任务\n"
    "/id — 查看当前 chat id 和你的 user id\n"
    "/help — 显示本帮助\n\n"
    "支持：X（x.com / twitter.com）与 t.me 消息链接。\n"
    "上传走 MTProto，单文件上限 2000 MiB，不受 Bot API 50 MiB 限制。"
)

STATE_ICONS = {
    TaskState.QUEUED: "⏳",
    TaskState.RUNNING: "⚙️",
    TaskState.PAUSED: "⏸",
}


class SyncError(RuntimeError):
    """处理一条命令时的可预期失败。"""


class SyncApp:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = TelegramClient(
            config.session,
            config.api_id,
            config.api_hash,
            proxy=config.telethon_proxy(),
        )
        self.store = TaskStore(config.task_db)
        self.uploader = Uploader(
            self.client,
            max_file_size=config.max_file_size,
            album_limit=config.album_limit,
        )
        self.x_downloader = XDownloader(
            cookies=config.ytdlp_cookies,
            cookies_from_browser=config.ytdlp_cookies_from_browser,
            proxy=config.proxy,
            max_file_size=config.max_file_size,
        )
        self.tg_downloader = TelegramDownloader(self.client)
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=config.queue_size)
        self.bot_id: int | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ 生命周期

    async def run(self) -> None:
        self.config.ensure_dirs()
        await self.client.start(bot_token=self.config.bot_token)
        me = await self.client.get_me()
        self.bot_id = me.id

        limit = (
            PREMIUM_MAX_FILE_SIZE
            if getattr(me, "premium", False)
            else self.config.max_file_size
        )
        self.uploader.max_file_size = limit

        self.client.add_event_handler(
            self._on_new_message, events.NewMessage(incoming=True)
        )
        self.client.add_event_handler(self._on_callback, events.CallbackQuery())

        self._cleanup_stale_dirs()
        restored = self._restore_tasks()
        workers = [
            asyncio.create_task(self._worker(), name=f"tg-sync-worker-{index}")
            for index in range(self.config.workers)
        ]
        log.info(
            "机器人 @%s 已启动（id=%s），上传上限 %.0f MiB，并发 %s，恢复任务 %s 个，"
            "结果发送到：%s",
            me.username or "?",
            me.id,
            limit / MIB,
            self.config.workers,
            restored,
            self.config.target_chat or "命令所在聊天",
        )
        if not self.config.allowed_users:
            log.warning(
                "TG_ALLOWED_USERS 为空：所有用户都会被拒绝。"
                "先给机器人发 /start 拿到 user id，再写入 .env"
            )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)

        client_task = asyncio.create_task(self.client.run_until_disconnected())
        stop_task = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait(
                {client_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop.is_set():
                log.info(
                    "收到退出信号，等待进行中的任务完成（最多 %ss）…",
                    self.config.shutdown_grace,
                )
                await self._drain(self.config.shutdown_grace)
        finally:
            stop_task.cancel()
            for worker in workers:
                worker.cancel()
            for worker in workers:
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
            with contextlib.suppress(Exception):
                await self.client.disconnect()
            client_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await client_task
            self.store.close()

    def _cleanup_stale_dirs(self) -> None:
        """启动时清理上次崩溃/强杀留下的下载目录（此时还没有任务在跑）。"""
        for path in self.config.download_dir.glob("task-*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                log.info("清理残留下载目录：%s", path.name)

    def _restore_tasks(self) -> int:
        """把上次中断的任务放回队列；暂停的保持暂停。"""
        self.store.requeue_running()
        queued = self.store.queued()
        for task in queued:
            try:
                self.queue.put_nowait(task.id)
            except asyncio.QueueFull:
                log.warning("队列已满，任务 #%s 暂不恢复", task.id)
                break
        return len(queued)

    async def _drain(self, grace: int) -> None:
        """等待进行中的任务完成；排队中的任务留在库里，重启后继续。"""
        running = [task for task in self._running.values() if not task.done()]
        if running:
            await asyncio.wait(running, timeout=grace)

    # ------------------------------------------------------------------ 命令入口

    async def _on_new_message(self, event) -> None:
        text = event.raw_text or ""
        name = command_name(text)
        if name is None:
            return

        if not self._authorized(event.sender_id):
            log.info(
                "拒绝未授权用户 %s（chat %s）", event.sender_id, event.chat_id
            )
            await self._reply(event, self._unauthorized_hint(event.sender_id))
            return

        if name in {"start", "help"}:
            await self._reply(event, HELP_TEXT)
            return
        if name == "id":
            await self._reply(
                event, f"chat_id: {event.chat_id}\nuser_id: {event.sender_id}"
            )
            return
        if name == "tasks":
            text, buttons = self._task_list_payload()
            await self._reply(event, text, buttons=buttons)
            return
        if name == "retry":
            await self._handle_retry_command(event, text)
            return

        try:
            command = parse_command(text)
        except CommandError as exc:
            await self._reply(event, f"❌ {exc}")
            return
        if command is None:
            return

        existing = self.store.find_by_message(event.chat_id, event.message.id)
        if existing is not None:
            log.debug("跳过已存在的任务 #%s", existing.id)
            return

        upload_chat = (
            self.config.target_chat
            if self.config.target_chat is not None
            else event.chat_id
        )
        task = self.store.add(
            chat_id=event.chat_id,
            message_id=event.message.id,
            urls=command.urls,
            upload_chat=upload_chat,
            reply_to=event.message.id if upload_chat == event.chat_id else None,
            force_download=command.force_download,
        )
        await self._send_status(task)

        try:
            self.queue.put_nowait(task.id)
        except asyncio.QueueFull:
            self.store.set_state(task.id, TaskState.FAILED, "队列已满")
            await self._refresh_task_message(task.id, "⏳ 队列已满，请稍后再试")
            return
        log.info("收到命令 #%s：%s", task.id, " ".join(command.urls))

    def _authorized(self, sender_id: int | None) -> bool:
        return bool(self.config.allowed_users) and sender_id in self.config.allowed_users

    def _unauthorized_hint(self, sender_id: int | None) -> str:
        if not self.config.allowed_users:
            return (
                "⛔ 机器人还没有配置授权用户。\n"
                f"你的 user id 是 {sender_id}，请在 .env 中设置：\n"
                f"TG_ALLOWED_USERS={sender_id}"
            )
        return (
            "⛔ 你没有使用权限。\n"
            f"你的 user id 是 {sender_id}，请让管理员把它加入 TG_ALLOWED_USERS"
        )

    # ------------------------------------------------------------------ 按钮回调

    async def _on_callback(self, event) -> None:
        if not self._authorized(event.sender_id):
            await event.answer("没有权限", alert=True)
            return

        action, _, task_id = (event.data or b"").decode().partition(":")
        if action == "list":
            await event.answer()
            text, buttons = self._task_list_payload()
            with contextlib.suppress(Exception):
                await event.edit(text, buttons=buttons)
            return

        task = self.store.get(task_id)
        if task is None:
            await event.answer("任务不存在（可能已清理）", alert=True)
            return

        if action == "pause":
            changed = await self._pause_task(task)
            await event.answer("已暂停" if changed else "任务已结束")
        elif action == "resume":
            changed = await self._resume_task(task)
            await event.answer("已继续" if changed else "任务不在暂停状态")
        elif action == "cancel":
            changed = await self._cancel_task(task)
            await event.answer("已取消" if changed else "任务已结束")
        elif action == "retry":
            changed = await self._retry_task(task)
            await event.answer("已重新排队" if changed else "任务还在进行中")
        else:
            await event.answer("未知操作", alert=True)
            return

        await self._refresh_task_message(task.id)

    async def _pause_task(self, task: Task) -> bool:
        if task.state in TERMINAL_STATES or task.state == TaskState.PAUSED:
            return False
        was_running = task.state == TaskState.RUNNING
        self.store.set_state(task.id, TaskState.PAUSED, "⏸ 已暂停")
        if was_running:
            await self._stop_runner(task.id)
        log.info("暂停任务 #%s", task.id)
        return True

    async def _resume_task(self, task: Task) -> bool:
        if task.state != TaskState.PAUSED:
            return False
        self.store.set_state(task.id, TaskState.QUEUED, "⏳ 排队中")
        try:
            self.queue.put_nowait(task.id)
        except asyncio.QueueFull:
            self.store.set_state(task.id, TaskState.PAUSED, "⏸ 队列已满，稍后重试")
            return False
        log.info("继续任务 #%s", task.id)
        return True

    async def _cancel_task(self, task: Task) -> bool:
        if task.state in TERMINAL_STATES:
            return False
        was_running = task.state == TaskState.RUNNING
        self.store.set_state(task.id, TaskState.CANCELLED, "✖ 已取消")
        if was_running:
            await self._stop_runner(task.id)
        log.info("取消任务 #%s", task.id)
        return True

    async def _retry_task(self, task: Task) -> bool:
        if task.state not in TERMINAL_STATES:
            return False
        previous = task.state
        self.store.set_state(task.id, TaskState.QUEUED, "⏳ 排队中（重试）", progress=-1)
        try:
            self.queue.put_nowait(task.id)
        except asyncio.QueueFull:
            self.store.set_state(task.id, previous, "队列已满，稍后再试")
            return False
        log.info("重试任务 #%s", task.id)
        return True

    async def _handle_retry_command(self, event, text: str) -> None:
        parts = text.split(maxsplit=1)
        task_id = parts[1].strip().lstrip("#") if len(parts) > 1 else ""
        if not task_id:
            await self._reply(
                event,
                "用法：/retry <任务ID>\n任务 ID 在状态消息里（🆔 a1b2c3d4），"
                "失败消息上也有「🔄 重试」按钮。",
            )
            return
        task = self.store.get(task_id)
        if task is None:
            await self._reply(event, f"❌ 找不到任务 #{task_id}")
            return
        if await self._retry_task(task):
            await self._reply(event, f"🔄 已重新排队 #{task_id}")
            await self._refresh_task_message(task_id)
        else:
            await self._reply(
                event, f"任务 #{task_id} 当前状态是 {task.state.value}，无需重试"
            )

    async def _stop_runner(self, task_id: str) -> None:
        runner = self._running.get(task_id)
        if runner is None or runner.done():
            return
        runner.cancel()
        await asyncio.wait({runner})

    # ------------------------------------------------------------------ 队列

    async def _worker(self) -> None:
        while True:
            task_id = await self.queue.get()
            try:
                await self._run_job(task_id)
            finally:
                self.queue.task_done()

    async def _run_job(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if task is None or task.state != TaskState.QUEUED:
            return

        self.store.set_state(task.id, TaskState.RUNNING, "🔍 开始处理…")
        task.state = TaskState.RUNNING
        task.detail = "🔍 开始处理…"

        runner = asyncio.current_task()
        if runner is not None:
            self._running[task.id] = runner
        try:
            timeout = self.config.job_timeout
            if timeout > 0:
                await asyncio.wait_for(self._process(task), timeout=timeout)
            else:
                await self._process(task)
        except asyncio.TimeoutError:
            log.warning("任务 #%s 超时（%ss）", task.id, timeout)
            minutes = max(1, round(timeout / 60))
            self.store.set_state(task.id, TaskState.FAILED, f"处理超时（{minutes} 分钟）")
            await self._refresh_task_message(
                task.id, f"❌ 处理超时（超过 {minutes} 分钟），请重试"
            )
        except asyncio.CancelledError:
            current = self.store.get(task.id)
            if current is not None and current.state == TaskState.RUNNING:
                # 不是用户暂停/取消，而是进程退出：保留任务，重启后继续
                self.store.set_state(
                    task.id, TaskState.QUEUED, "⏳ 已中断，重启后继续"
                )
            raise
        finally:
            self._running.pop(task.id, None)

    async def _process(self, task: Task) -> None:
        self.config.ensure_dirs()
        status = await self._ensure_status(task)
        workdir = Path(
            tempfile.mkdtemp(
                prefix=f"task-{task.id}-", dir=str(self.config.download_dir)
            )
        )
        items: list[MediaItem] = []
        errors: list[str] = []

        try:
            forwarded = 0
            for raw in task.urls:
                try:
                    link = parse_link(raw)
                except LinkError as exc:
                    errors.append(f"{raw}\n  ↳ {exc}")
                    continue

                try:
                    if link.kind is LinkKind.TELEGRAM and not task.force_download:
                        # 能转发就不下载：省带宽、保原画质，相册也能整组搬
                        await self._edit_status(
                            task, status, f"↪️ 转发中：{link.describe()}"
                        )
                        count = await self.tg_downloader.transfer(
                            link, task.upload_chat, task.reply_to
                        )
                        if count:
                            forwarded += count
                            continue
                    await self._edit_status(
                        task, status, f"⬇️ 下载中：{link.describe()}"
                    )
                    items.extend(
                        await self._downloader_for(link).download(link, workdir)
                    )
                except DownloadError as exc:
                    log.warning("处理失败 %s：%s", raw, exc)
                    errors.append(f"{raw}\n  ↳ {exc}")

            uploaded = 0
            skipped: list[MediaItem] = []
            if items:
                await self._edit_status(
                    task, status, f"⬆️ 上传中：{len(items)} 个文件…"
                )
                result = await self.uploader.upload(
                    items,
                    chat=task.upload_chat,
                    reply_to=task.reply_to,
                    progress=self._progress_callback(task, status),
                )
                uploaded = result.uploaded
                skipped = result.skipped

            total = forwarded + uploaded
            if total == 0:
                detail = "\n".join(errors)
                raise SyncError("没有可上传的媒体" + (f"\n{detail}" if detail else ""))

            if errors or skipped:
                summary = self._summary(total, skipped, errors)
                self.store.set_state(task.id, TaskState.DONE, summary)
                task.state = TaskState.DONE
                await self._finish_status(task, status, summary)
            else:
                done = f"✅ 完成：{total} 个文件"
                self.store.set_state(task.id, TaskState.DONE, done)
                await self._delete_status(status)
            log.info(
                "完成 #%s：发送 %s 个文件（转发 %s），失败链接 %s 个",
                task.id,
                total,
                forwarded,
                len(errors),
            )

        except (SyncError, UploadError) as exc:
            self.store.set_state(task.id, TaskState.FAILED, str(exc))
            task.state = TaskState.FAILED
            await self._finish_status(task, status, f"❌ {exc}")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("处理 #%s 失败", task.id)
            self.store.set_state(task.id, TaskState.FAILED, "内部错误")
            task.state = TaskState.FAILED
            await self._finish_status(task, status, "❌ 内部错误，详见日志")
        finally:
            if self.config.keep_files:
                log.info("保留下载目录：%s", workdir)
            else:
                shutil.rmtree(workdir, ignore_errors=True)

    def _downloader_for(self, link: Link):
        if link.kind is LinkKind.X:
            return self.x_downloader
        if link.kind is LinkKind.TELEGRAM:
            return self.tg_downloader
        raise SyncError(f"暂不支持的链接类型：{link.kind}")

    # ------------------------------------------------------------------ 状态消息

    def _status_text(self, task: Task) -> str:
        return f"🆔 {task.id} · {task.detail}"

    def _buttons(self, task: Task) -> list[list[Any]] | None:
        if task.state in TERMINAL_STATES:
            return [[Button.inline("🔄 重试", f"retry:{task.id}".encode())]]
        if task.state == TaskState.PAUSED:
            first = Button.inline("▶️ 继续", f"resume:{task.id}".encode())
        else:
            first = Button.inline("⏸ 暂停", f"pause:{task.id}".encode())
        return [[first, Button.inline("✖ 取消", f"cancel:{task.id}".encode())]]

    async def _send_status(self, task: Task) -> Any | None:
        if not self.config.status_messages:
            return None
        try:
            message = await self.client.send_message(
                task.chat_id,
                self._status_text(task),
                reply_to=task.message_id,
                buttons=self._buttons(task),
            )
        except Exception as exc:
            log.warning("发送状态消息失败（忽略）：%s", exc)
            return None
        self.store.set_status_message(task.id, message.id)
        task.status_message_id = message.id
        return message

    async def _ensure_status(self, task: Task) -> Any | None:
        """重启后尽量复用原来的状态消息；被删了就重新发一条。"""
        if not self.config.status_messages:
            return None
        if task.status_message_id:
            try:
                message = await self.client.get_messages(
                    task.chat_id, ids=task.status_message_id
                )
            except Exception as exc:
                log.debug("读取状态消息失败：%s", exc)
                message = None
            if message is not None:
                return message
        return await self._send_status(task)

    async def _edit_status(self, task: Task, status, detail: str | None = None) -> None:
        if detail is not None:
            task.detail = detail
            self.store.set_detail(task.id, detail)
        if status is None:
            return
        try:
            await status.edit(self._status_text(task), buttons=self._buttons(task))
        except Exception as exc:
            log.debug("更新状态消息失败（忽略）：%s", exc)

    async def _finish_status(self, task: Task, status, text: str) -> None:
        if status is None:
            return
        try:
            await status.edit(text, buttons=self._buttons(task))
        except Exception as exc:
            log.debug("更新状态消息失败（忽略）：%s", exc)

    async def _delete_status(self, status) -> None:
        if status is None:
            return
        try:
            await status.delete()
        except Exception as exc:
            log.debug("删除状态消息失败（忽略）：%s", exc)

    async def _refresh_task_message(self, task_id: str, detail: str | None = None) -> None:
        task = self.store.get(task_id)
        if task is None:
            return
        if detail is not None:
            task.detail = detail
        status = await self._ensure_status(task)
        if status is None:
            return
        try:
            await status.edit(self._status_text(task), buttons=self._buttons(task))
        except Exception as exc:
            log.debug("刷新状态消息失败（忽略）：%s", exc)

    # ------------------------------------------------------------------ 任务列表

    def _task_list_payload(self) -> tuple[str, list[list[Any]] | None]:
        tasks = self.store.active()
        if not tasks:
            return "当前没有进行中的任务", None

        lines = [f"📋 进行中的任务（{len(tasks)}）"]
        rows: list[list[Any]] = []
        for task in tasks[:MAX_LISTED_TASKS]:
            icon = STATE_ICONS.get(task.state, "")
            lines.append(f"#{task.id} {icon} {task.detail}")
            lines.append(f"   {task.link_summary}")
            if task.state == TaskState.PAUSED:
                first = Button.inline(f"▶️ {task.id}", f"resume:{task.id}".encode())
            else:
                first = Button.inline(f"⏸ {task.id}", f"pause:{task.id}".encode())
            rows.append(
                [first, Button.inline(f"✖ {task.id}", f"cancel:{task.id}".encode())]
            )
        if len(tasks) > MAX_LISTED_TASKS:
            lines.append(f"…还有 {len(tasks) - MAX_LISTED_TASKS} 个任务")
        rows.append([Button.inline("🔄 刷新", b"list")])
        return "\n".join(lines), rows

    # ------------------------------------------------------------------ 辅助

    async def _reply(self, event, text: str, buttons=None) -> None:
        try:
            await event.reply(text, buttons=buttons)
        except Exception:
            log.exception("回复消息失败")

    def _summary(
        self, total: int, skipped: list[MediaItem], errors: list[str]
    ) -> str:
        lines = [f"✅ 已发送 {total} 个文件"]
        if skipped:
            lines.append(
                f"⚠️ 跳过 {len(skipped)} 个超过 "
                f"{self.uploader.max_file_size / MIB:.0f} MiB 的文件"
            )
        if errors:
            lines.append("❌ 以下链接失败：")
            lines.extend(f"• {error}" for error in errors)
        return "\n".join(lines)[:4000]

    def _progress_callback(self, task: Task, status):
        if status is None:
            return None
        state = {"last": 0.0}

        def callback(current: int, total: int) -> None:
            if not total:
                return
            now = time.monotonic()
            if now - state["last"] < 3:
                return
            state["last"] = now
            percent = min(100, int(current * 100 / total))
            detail = f"⬆️ 上传中：{percent}%"
            task.detail = detail
            self.store.set_detail(task.id, detail, progress=percent)
            with contextlib.suppress(RuntimeError):  # 事件循环正在关闭
                edit = asyncio.create_task(self._edit_status(task, status))
                self._bg_tasks.add(edit)
                edit.add_done_callback(self._bg_tasks.discard)

        return callback
