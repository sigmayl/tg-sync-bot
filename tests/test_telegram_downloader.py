from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.errors import ChatForwardsRestrictedError

from tg_sync.downloaders.base import DownloadError
from tg_sync.downloaders.telegram import TelegramDownloader
from tg_sync.links import parse_link


class FakeMessage:
    def __init__(
        self,
        message_id: int,
        *,
        grouped_id: int | None = None,
        name: str | None = "photo.jpg",
        ext: str = "jpg",
        text: str = "",
    ) -> None:
        self.id = message_id
        self.grouped_id = grouped_id
        self.file = (
            SimpleNamespace(name=name, ext=ext, size=10) if name is not None else None
        )
        self.raw_text = text


class FakeClient:
    def __init__(self, single=None, neighbours=None) -> None:
        self.single = single
        self.neighbours = neighbours or []
        self.downloads: list[Path] = []
        self.forwarded = None

    async def get_entity(self, peer):
        return "entity"

    async def get_input_entity(self, peer):
        return peer

    async def __call__(self, request):
        self.forwarded = request
        return SimpleNamespace(updates=[])

    async def get_messages(self, entity, ids=None, **kwargs):
        if isinstance(ids, list):
            by_id = {message.id: message for message in self.neighbours}
            return [by_id.get(message_id) for message_id in ids]
        if ids is not None:
            return self.single
        return self.neighbours

    async def download_media(self, message, file=None):
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
        self.downloads.append(path)
        return str(path)


async def test_single_message(tmp_path: Path) -> None:
    message = FakeMessage(10, name="clip.mp4", ext="mp4", text="标题")
    downloader = TelegramDownloader(FakeClient(single=message))

    items = await downloader.download(parse_link("https://t.me/durov/10"), tmp_path)

    assert len(items) == 1
    assert items[0].path.name == "01_clip.mp4"
    assert items[0].title == "标题"
    assert items[0].source_url == "https://t.me/durov/10"


async def test_album_is_expanded(tmp_path: Path) -> None:
    target = FakeMessage(11, grouped_id=7, name="b.jpg")
    neighbours = [
        FakeMessage(10, grouped_id=7, name="a.jpg"),
        target,
        FakeMessage(12, grouped_id=7, name="c.jpg"),
        FakeMessage(13, grouped_id=8, name="other.jpg"),
    ]
    downloader = TelegramDownloader(FakeClient(single=target, neighbours=neighbours))

    items = await downloader.download(parse_link("https://t.me/durov/11"), tmp_path)

    assert [item.path.name for item in items] == ["01_a.jpg", "02_b.jpg", "03_c.jpg"]


async def test_unsafe_filename_is_sanitized(tmp_path: Path) -> None:
    message = FakeMessage(5, name='../evil:name?.mp4', ext="mp4")
    downloader = TelegramDownloader(FakeClient(single=message))

    items = await downloader.download(parse_link("https://t.me/durov/5"), tmp_path)

    assert items[0].path.parent == tmp_path
    assert "/" not in items[0].path.name
    assert ":" not in items[0].path.name


async def test_missing_extension_is_added(tmp_path: Path) -> None:
    message = FakeMessage(5, name="noext", ext="jpg")
    downloader = TelegramDownloader(FakeClient(single=message))

    items = await downloader.download(parse_link("https://t.me/durov/5"), tmp_path)

    assert items[0].path.name == "01_noext.jpg"


async def test_message_without_media_raises(tmp_path: Path) -> None:
    message = FakeMessage(5, name=None)
    downloader = TelegramDownloader(FakeClient(single=message))

    with pytest.raises(DownloadError, match="不包含可下载的媒体"):
        await downloader.download(parse_link("https://t.me/durov/5"), tmp_path)


async def test_deleted_message_raises(tmp_path: Path) -> None:
    downloader = TelegramDownloader(FakeClient(single=None))

    with pytest.raises(DownloadError, match="找不到该消息"):
        await downloader.download(parse_link("https://t.me/durov/5"), tmp_path)


async def test_unreachable_channel_raises(tmp_path: Path) -> None:
    class BrokenClient(FakeClient):
        async def get_entity(self, peer):
            raise RuntimeError("ChannelPrivateError")

    downloader = TelegramDownloader(BrokenClient())
    with pytest.raises(DownloadError, match="无法访问"):
        await downloader.download(parse_link("https://t.me/durov/5"), tmp_path)


async def test_private_channel_refreshes_dialogs(tmp_path: Path) -> None:
    """冷启动时私有频道不在缓存里，应该拉一次对话框后重试。"""

    class ColdClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(single=FakeMessage(10))
            self.attempts = 0
            self.dialogs_fetched = 0

        async def get_entity(self, peer):
            self.attempts += 1
            if self.attempts == 1:
                raise ValueError("Could not find the input entity for PeerChannel")
            return "entity"

        async def get_dialogs(self, limit=None):
            self.dialogs_fetched += 1
            return []

    client = ColdClient()
    items = await TelegramDownloader(client).download(
        parse_link("https://t.me/c/123/10"), tmp_path
    )

    assert client.dialogs_fetched == 1
    assert len(items) == 1


async def test_private_channel_fails_after_refresh(tmp_path: Path) -> None:
    class AlwaysBroken(FakeClient):
        async def get_entity(self, peer):
            raise ValueError("still not found")

        async def get_dialogs(self, limit=None):
            return []

    with pytest.raises(DownloadError, match="无法访问"):
        await TelegramDownloader(AlwaysBroken()).download(
            parse_link("https://t.me/c/123/10"), tmp_path
        )


# --------------------------------------------------------------------- 直接转发


async def test_transfer_forwards_album_without_downloading() -> None:
    target = FakeMessage(11, grouped_id=7, name="b.jpg")
    neighbours = [
        FakeMessage(10, grouped_id=7, name="a.jpg"),
        target,
        FakeMessage(12, grouped_id=7, name="c.jpg"),
        FakeMessage(13, grouped_id=8, name="other.jpg"),
    ]
    client = FakeClient(single=target, neighbours=neighbours)

    count = await TelegramDownloader(client).transfer(
        parse_link("https://t.me/durov/11"), 999, reply_to=42
    )

    assert count == 3
    request = client.forwarded
    assert request.drop_author is True
    assert request.id == [10, 11, 12]
    assert request.reply_to.reply_to_msg_id == 42
    assert client.downloads == []


async def test_transfer_returns_none_when_forwards_restricted() -> None:
    class Restricted(FakeClient):
        async def __call__(self, request):
            raise ChatForwardsRestrictedError(request=request)

    client = Restricted(single=FakeMessage(5))
    assert (
        await TelegramDownloader(client).transfer(
            parse_link("https://t.me/durov/5"), 999
        )
        is None
    )


async def test_transfer_returns_none_without_media() -> None:
    client = FakeClient(single=FakeMessage(5, name=None))
    assert (
        await TelegramDownloader(client).transfer(
            parse_link("https://t.me/durov/5"), 999
        )
        is None
    )
