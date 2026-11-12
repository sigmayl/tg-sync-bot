"""上传：把下载好的文件发到目标聊天（默认回复命令消息）。

视频会额外带上 ``DocumentAttributeVideo``（时长/分辨率）和 ffmpeg 抽的封面；
否则 Telegram 客户端不显示时长和缩略图。

- 单文件：Telethon ``send_file``（支持 attributes/thumb）
- 相册：Telethon 的相册路径不接受 attributes/thumb，所以手动走
  ``messages.uploadMedia`` + ``messages.sendMultiMedia``，逐条带元数据。
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import random
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telethon import utils
from telethon.tl import functions, types

from .media import extract_thumbnail, probe_video
from .models import MediaItem

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
MIB = 1024 * 1024

ProgressCallback = Callable[[int, int], None]


class UploadError(RuntimeError):
    """上传失败（消息会直接展示给用户）。"""


@dataclass(slots=True)
class UploadResult:
    messages: list[Any] = field(default_factory=list)
    skipped: list[MediaItem] = field(default_factory=list)

    @property
    def uploaded(self) -> int:
        return len(self.messages)


@dataclass(slots=True)
class _Prepared:
    mime_type: str
    attributes: list[Any] | None = None
    thumb: Path | None = None


class Uploader:
    def __init__(
        self,
        client,
        *,
        max_file_size: int,
        album_limit: int = 10,
        caption_limit: int = CAPTION_LIMIT,
    ) -> None:
        self.client = client
        self.max_file_size = max_file_size
        self.album_limit = max(1, min(10, album_limit))
        self.caption_limit = caption_limit

    async def upload(
        self,
        items: Sequence[MediaItem],
        *,
        chat: int | str,
        reply_to: int | None = None,
        progress: ProgressCallback | None = None,
    ) -> UploadResult:
        usable = [item for item in items if item.size <= self.max_file_size]
        skipped = [item for item in items if item.size > self.max_file_size]
        for item in skipped:
            log.warning(
                "跳过超过上传上限的文件 %s (%.1f MiB)",
                item.display_name,
                item.size / MIB,
            )
        if not usable:
            raise UploadError(
                f"所有文件都超过上传上限 {self.max_file_size / MIB:.0f} MiB"
            )

        result = UploadResult(skipped=skipped)
        for index, chunk in enumerate(_chunked(usable, self.album_limit)):
            caption = self._caption(chunk) if index == 0 else None
            try:
                sent = await self._send(
                    chat,
                    chunk,
                    caption=caption,
                    reply_to=reply_to,
                    progress=progress,
                )
            except Exception as exc:
                raise UploadError(f"上传失败：{exc}") from exc
            result.messages.extend(sent)
        return result

    async def _send(
        self,
        chat: int | str,
        chunk: Sequence[MediaItem],
        *,
        caption: str | None,
        reply_to: int | None,
        progress: ProgressCallback | None,
    ) -> list[Any]:
        if len(chunk) == 1:
            message = await self._send_single(
                chat, chunk[0], caption=caption, reply_to=reply_to, progress=progress
            )
            return [message]
        return await self._send_album(
            chat, chunk, caption=caption, reply_to=reply_to, progress=progress
        )

    async def _send_single(
        self,
        chat: int | str,
        item: MediaItem,
        *,
        caption: str | None,
        reply_to: int | None,
        progress: ProgressCallback | None,
    ) -> Any:
        prepared = await self._prepare(item)
        kwargs: dict[str, Any] = {
            "reply_to": reply_to,
            "supports_streaming": True,
            "progress_callback": progress,
        }
        if prepared.attributes:
            kwargs["attributes"] = prepared.attributes
        if prepared.thumb:
            kwargs["thumb"] = str(prepared.thumb)
        return await self.client.send_file(
            chat, str(item.path), caption=caption or "", **kwargs
        )

    async def _send_album(
        self,
        chat: int | str,
        chunk: Sequence[MediaItem],
        *,
        caption: str | None,
        reply_to: int | None,
        progress: ProgressCallback | None,
    ) -> list[Any]:
        peer = await self.client.get_input_entity(chat)
        multi: list[types.InputSingleMedia] = []
        for index, item in enumerate(chunk):
            prepared = await self._prepare(item)
            uploaded = await self.client.upload_file(
                str(item.path), progress_callback=progress
            )
            media = await self._build_media(item, uploaded, prepared)
            # 相册里必须先 uploadMedia 换成真正的 photo/document 引用
            uploaded_media = await self.client(
                functions.messages.UploadMediaRequest(peer, media=media)
            )
            if isinstance(uploaded_media, types.MessageMediaPhoto):
                input_media = utils.get_input_media(uploaded_media.photo)
            else:
                input_media = utils.get_input_media(
                    uploaded_media.document, supports_streaming=True
                )
            multi.append(
                types.InputSingleMedia(
                    media=input_media,
                    message=(caption or "") if index == 0 else "",
                    random_id=random.getrandbits(63),
                )
            )

        request = functions.messages.SendMultiMediaRequest(
            peer=peer,
            multi_media=multi,
            reply_to=(
                types.InputReplyToMessage(reply_to_msg_id=reply_to)
                if reply_to
                else None
            ),
        )
        result = await self.client(request)
        messages = self.client._get_response_message(
            [item.random_id for item in multi], result, peer
        )
        return list(messages) if messages else [None] * len(multi)

    async def _build_media(
        self, item: MediaItem, uploaded: Any, prepared: _Prepared
    ) -> Any:
        if item.kind == "photo":
            return types.InputMediaUploadedPhoto(file=uploaded)

        attributes: list[Any] = [types.DocumentAttributeFilename(item.path.name)]
        if prepared.attributes:
            attributes.extend(prepared.attributes)
        thumb_input = None
        if prepared.thumb:
            thumb_input = await self.client.upload_file(str(prepared.thumb))
        return types.InputMediaUploadedDocument(
            file=uploaded,
            mime_type=prepared.mime_type,
            attributes=attributes,
            thumb=thumb_input,
        )

    async def _prepare(self, item: MediaItem) -> _Prepared:
        mime_type = (
            mimetypes.guess_type(item.path.name)[0] or "application/octet-stream"
        )
        if item.kind != "video":
            return _Prepared(mime_type=mime_type)

        meta = await asyncio.to_thread(probe_video, item.path)
        thumb_path = item.path.with_name(item.path.stem + ".thumb.jpg")
        thumb = await asyncio.to_thread(extract_thumbnail, item.path, thumb_path)
        attributes = None
        if meta is not None:
            attributes = [
                types.DocumentAttributeVideo(
                    duration=meta.duration,
                    w=meta.width,
                    h=meta.height,
                    supports_streaming=True,
                )
            ]
        return _Prepared(mime_type=mime_type, attributes=attributes, thumb=thumb)

    def _caption(self, chunk: Sequence[MediaItem]) -> str | None:
        parts: list[str] = []
        title = (chunk[0].title or "").strip()
        if title:
            parts.append(title)
        if chunk[0].source_url:
            parts.append(chunk[0].source_url)
        text = "\n".join(parts).strip()
        if len(text) > self.caption_limit:
            text = text[: self.caption_limit - 1] + "…"
        return text or None


def _chunked(items: Iterable[MediaItem], size: int) -> Iterable[list[MediaItem]]:
    chunk: list[MediaItem] = []
    for item in items:
        chunk.append(item)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
