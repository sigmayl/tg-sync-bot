from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl import functions, types

from tg_sync.models import MediaItem
from tg_sync.uploader import Uploader, UploadError

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class FakeMessage:
    def __init__(self, index: int) -> None:
        self.index = index


def _photo() -> types.Photo:
    return types.Photo(
        id=1,
        access_hash=2,
        file_reference=b"",
        date=datetime.now(),
        sizes=[],
        dc_id=2,
    )


def _document() -> types.Document:
    return types.Document(
        id=3,
        access_hash=4,
        file_reference=b"",
        date=datetime.now(),
        mime_type="video/mp4",
        size=10,
        dc_id=2,
        attributes=[],
    )


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.uploads: list[str] = []
        self.requests: list[object] = []

    async def send_file(self, entity, file, **kwargs):
        self.calls.append({"entity": entity, "file": file, "kwargs": kwargs})
        if isinstance(file, list):
            return [FakeMessage(i) for i in range(len(file))]
        return FakeMessage(0)

    async def get_input_entity(self, peer):
        return peer

    async def upload_file(self, file, progress_callback=None):
        self.uploads.append(str(file))
        return types.InputFile(
            id=len(self.uploads), parts=1, name=Path(file).name, md5_checksum=""
        )

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.messages.UploadMediaRequest):
            if isinstance(request.media, types.InputMediaUploadedPhoto):
                return types.MessageMediaPhoto(photo=_photo())
            return types.MessageMediaDocument(document=_document())
        if isinstance(request, functions.messages.SendMultiMediaRequest):
            return SimpleNamespace(updates=[])
        raise AssertionError(f"unexpected request {request!r}")

    def _get_response_message(self, random_ids, result, peer):
        return [FakeMessage(i) for i in range(len(random_ids))]


def make_item(tmp_path: Path, name: str, size: int = 10, **kwargs) -> MediaItem:
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return MediaItem(path, **kwargs)


def make_video(tmp_path: Path, name: str = "clip.mp4", seconds: int = 2) -> MediaItem:
    path = tmp_path / name
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x240:rate=10:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return MediaItem(path)


def album_requests(client: FakeClient) -> list:
    return [
        request
        for request in client.requests
        if isinstance(request, functions.messages.SendMultiMediaRequest)
    ]


def upload_requests(client: FakeClient) -> list:
    return [
        request
        for request in client.requests
        if isinstance(request, functions.messages.UploadMediaRequest)
    ]


async def test_single_file_replies_to_command(tmp_path: Path) -> None:
    client = FakeClient()
    uploader = Uploader(client, max_file_size=1024)
    item = make_item(
        tmp_path, "a.jpg", title="hello world", source_url="https://x.com/a/status/1"
    )

    result = await uploader.upload([item], chat=99, reply_to=42)

    assert result.uploaded == 1
    call = client.calls[0]
    assert call["entity"] == 99
    assert call["file"] == str(item.path)
    assert call["kwargs"]["reply_to"] == 42
    assert call["kwargs"]["supports_streaming"] is True
    assert call["kwargs"]["caption"] == "hello world\nhttps://x.com/a/status/1"
    assert "attributes" not in call["kwargs"]
    assert "thumb" not in call["kwargs"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg/ffprobe")
async def test_single_video_gets_duration_and_cover(tmp_path: Path) -> None:
    client = FakeClient()
    uploader = Uploader(client, max_file_size=10 * 1024 * 1024)
    item = make_video(tmp_path)

    await uploader.upload([item], chat=99)

    kwargs = client.calls[0]["kwargs"]
    video_attr = next(
        attr
        for attr in kwargs["attributes"]
        if isinstance(attr, types.DocumentAttributeVideo)
    )
    assert video_attr.duration == 2
    assert (video_attr.w, video_attr.h) == (320, 240)
    assert video_attr.supports_streaming is True

    thumb = Path(kwargs["thumb"])
    assert thumb.exists()
    assert thumb.read_bytes()[:2] == b"\xff\xd8"  # JPEG
    assert thumb.stat().st_size < 200 * 1024


async def test_album_uses_send_multi_media(tmp_path: Path) -> None:
    client = FakeClient()
    uploader = Uploader(client, max_file_size=1024, album_limit=10)
    items = [make_item(tmp_path, f"{i}.jpg", title="t") for i in range(12)]

    result = await uploader.upload(items, chat=99, reply_to=7)

    assert result.uploaded == 12
    assert client.calls == []  # 相册不走 send_file
    requests = album_requests(client)
    assert len(requests) == 2
    assert len(requests[0].multi_media) == 10
    assert len(requests[1].multi_media) == 2
    assert requests[0].multi_media[0].message == "t"
    assert requests[0].multi_media[1].message == ""
    assert requests[1].multi_media[0].message == ""
    assert requests[0].reply_to.reply_to_msg_id == 7
    assert len(upload_requests(client)) == 12


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg/ffprobe")
async def test_album_video_gets_attributes_and_thumb(tmp_path: Path) -> None:
    client = FakeClient()
    uploader = Uploader(client, max_file_size=10 * 1024 * 1024)
    video = make_video(tmp_path)
    photo = make_item(tmp_path, "p.jpg")

    await uploader.upload([video, photo], chat=99)

    media_requests = upload_requests(client)
    video_media = media_requests[0].media
    assert isinstance(video_media, types.InputMediaUploadedDocument)
    video_attr = next(
        attr
        for attr in video_media.attributes
        if isinstance(attr, types.DocumentAttributeVideo)
    )
    assert video_attr.duration == 2
    assert video_attr.w == 320
    assert video_media.thumb is not None
    assert isinstance(media_requests[1].media, types.InputMediaUploadedPhoto)


async def test_oversized_files_are_skipped(tmp_path: Path) -> None:
    client = FakeClient()
    uploader = Uploader(client, max_file_size=5)
    small = make_item(tmp_path, "small.jpg", size=4)
    big = make_item(tmp_path, "big.jpg", size=10)

    result = await uploader.upload([small, big], chat=99)

    assert result.uploaded == 1
    assert [item.path.name for item in result.skipped] == ["big.jpg"]
    assert client.calls[0]["file"] == str(small.path)


async def test_all_oversized_raises(tmp_path: Path) -> None:
    uploader = Uploader(FakeClient(), max_file_size=5)
    with pytest.raises(UploadError):
        await uploader.upload([make_item(tmp_path, "big.jpg", size=10)], chat=99)


async def test_caption_is_truncated(tmp_path: Path) -> None:
    client = FakeClient()
    uploader = Uploader(client, max_file_size=1024, caption_limit=10)
    item = make_item(tmp_path, "a.jpg", title="x" * 50)

    await uploader.upload([item], chat=99)

    caption = client.calls[0]["kwargs"]["caption"]
    assert len(caption) == 10
    assert caption.endswith("…")


async def test_no_caption_when_no_metadata(tmp_path: Path) -> None:
    client = FakeClient()
    uploader = Uploader(client, max_file_size=1024)
    await uploader.upload([make_item(tmp_path, "a.jpg")], chat=99)
    assert client.calls[0]["kwargs"]["caption"] == ""


async def test_no_reply_when_target_chat_differs(tmp_path: Path) -> None:
    client = FakeClient()
    uploader = Uploader(client, max_file_size=1024)
    await uploader.upload([make_item(tmp_path, "a.jpg")], chat=-100123, reply_to=None)
    assert client.calls[0]["entity"] == -100123
    assert client.calls[0]["kwargs"]["reply_to"] is None
