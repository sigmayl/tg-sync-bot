"""t.me 链接下载：直接用已登录账号读取消息并下载媒体。

支持：
  https://t.me/<频道名>/<消息ID>      公开频道 / 公开用户
  https://t.me/c/<内部ID>/<消息ID>    私有频道（账号需已加入）
相册（grouped_id）会把同组消息一起下载。
"""

from __future__ import annotations

import logging
import random
import re
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import ChatForwardsRestrictedError
from telethon.tl import types
from telethon.tl.functions.messages import ForwardMessagesRequest

from ..links import Link
from ..models import MediaItem
from .base import DownloadError

log = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_NAME = 120


class TelegramDownloader:
    name = "telegram"

    def __init__(self, client: TelegramClient) -> None:
        self.client = client

    async def transfer(
        self, link: Link, dest_chat: int | str, reply_to: int | None = None
    ) -> int | None:
        """尝试用服务端转发把消息搬到 dest_chat（不下载、不占带宽）。

        返回转发的消息条数；频道禁止转发（noforwards）或转发失败时返回 None，
        由调用方回落到「下载后上传」。
        """
        try:
            entity = await self._resolve(link)
            message = await self._get_message(entity, link)
            messages = await self._expand_album(entity, message)
            ids = [msg.id for msg in messages if getattr(msg, "file", None) is not None]
            if not ids:
                return None
            await self._forward(entity, ids, dest_chat, reply_to)
            log.info("已转发 %s 条消息：%s", len(ids), link.url)
            return len(ids)
        except DownloadError:
            raise
        except ChatForwardsRestrictedError:
            log.info("频道禁止转发，改为下载后上传：%s", link.url)
            return None
        except Exception as exc:
            log.warning("直接转发失败，改为下载后上传：%s", exc)
            return None

    async def _forward(
        self,
        entity,
        ids: list[int],
        dest_chat: int | str,
        reply_to: int | None,
    ) -> None:
        from_peer = await self.client.get_input_entity(entity)
        to_peer = await self.client.get_input_entity(dest_chat)
        kwargs = {
            "from_peer": from_peer,
            "id": ids,
            "to_peer": to_peer,
            # drop_author=True 等价于「复制」：不带 Forwarded from 抬头
            "drop_author": True,
            "random_id": [random.getrandbits(63) for _ in ids],
        }
        if reply_to:
            kwargs["reply_to"] = types.InputReplyToMessage(reply_to_msg_id=reply_to)
        await self.client(ForwardMessagesRequest(**kwargs))

    async def download(self, link: Link, workdir: Path) -> list[MediaItem]:
        entity = await self._resolve(link)
        message = await self._get_message(entity, link)
        messages = await self._expand_album(entity, message)

        items: list[MediaItem] = []
        for index, msg in enumerate(messages, 1):
            if getattr(msg, "file", None) is None:
                continue
            path = await self._download_one(msg, workdir, index)
            if path is not None:
                items.append(
                    MediaItem(path, source_url=link.url, title=_title(msg))
                )

        if not items:
            raise DownloadError("该消息不包含可下载的媒体")
        return items

    async def _resolve(self, link: Link):
        try:
            return await self._entity_for(link)
        except DownloadError:
            raise
        except Exception as exc:
            # 私有频道没有 access_hash，只能从本账号的对话框缓存里找。
            # 冷启动（新 session）时缓存是空的，先拉一次对话框再重试。
            if link.channel_id is None:
                raise DownloadError(f"无法访问 {link.url}：{_humanize(exc)}") from exc
            log.info("私有频道不在缓存中，刷新对话框后重试：%s", exc)
            try:
                await self.client.get_dialogs(limit=None)
                return await self._entity_for(link)
            except Exception as retry_exc:
                raise DownloadError(
                    f"无法访问 {link.url}：{_humanize(retry_exc)}"
                ) from retry_exc

    async def _entity_for(self, link: Link):
        if link.channel_id is not None:
            return await self.client.get_entity(types.PeerChannel(link.channel_id))
        return await self.client.get_entity(link.username)

    async def _get_message(self, entity, link: Link):
        try:
            message = await self.client.get_messages(entity, ids=link.message_id)
        except Exception as exc:
            raise DownloadError(
                f"读取消息失败：{_humanize(exc)}"
            ) from exc
        if message is None:
            raise DownloadError("找不到该消息（可能已删除或当前账号无权访问）")
        return message

    async def _expand_album(self, entity, message) -> list:
        grouped_id = getattr(message, "grouped_id", None)
        if not grouped_id:
            return [message]
        # 机器人不能调用 messages.getHistory（BotMethodInvalidError），
        # 所以用 channels.getMessages 按 id 列表取邻居，而不是 min_id/max_id。
        ids = list(range(max(1, message.id - 9), message.id + 10))
        try:
            neighbours = await self.client.get_messages(entity, ids=ids)
        except Exception as exc:
            log.debug("读取相册失败，只处理单条消息：%s", exc)
            return [message]

        group = [
            msg
            for msg in neighbours or []
            if msg is not None and getattr(msg, "grouped_id", None) == grouped_id
        ]
        if not any(msg.id == message.id for msg in group):
            group.append(message)
        return sorted(group, key=lambda msg: msg.id)

    async def _download_one(self, message, workdir: Path, index: int) -> Path | None:
        file_info = getattr(message, "file", None)
        ext = (getattr(file_info, "ext", None) or "").lstrip(".") or "bin"
        name = getattr(file_info, "name", None) or f"{message.id}.{ext}"
        name = _sanitize(name)
        if not Path(name).suffix:
            name = f"{name}.{ext}"

        dest = workdir / f"{index:02d}_{name}"
        try:
            path = await self.client.download_media(message, file=str(dest))
        except Exception as exc:
            log.warning("下载消息 %s 失败：%s", message.id, exc)
            return None
        if not path:
            return None
        return Path(path)


def _sanitize(name: str) -> str:
    cleaned = _UNSAFE_CHARS.sub("_", name).strip(" .")
    if len(cleaned) > _MAX_NAME:
        suffix = Path(cleaned).suffix
        cleaned = cleaned[: _MAX_NAME - len(suffix)] + suffix
    return cleaned or "file"


def _title(message) -> str | None:
    text = (getattr(message, "raw_text", None) or "").strip()
    return text[:200] or None


def _humanize(exc: Exception) -> str:
    """把 Telethon 的异常翻译成用户能看懂的话。"""
    name = type(exc).__name__
    mapping = {
        "ChannelPrivateError": "频道为私有，或机器人不是该频道成员（需把机器人加为成员/管理员）",
        "UsernameNotOccupiedError": "用户名不存在",
        "UsernameInvalidError": "用户名无效",
        "InviteHashExpiredError": "邀请链接已失效",
        "MessageIdInvalidError": "消息 ID 无效",
        "ChannelInvalidError": "频道不存在或机器人无法访问",
        "ValueError": "无法解析该频道，请确认机器人已加入该频道",
    }
    if name in mapping:
        return mapping[name]
    text = str(exc).strip()
    return text or name
