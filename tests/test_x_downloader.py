from __future__ import annotations

from pathlib import Path

import pytest

from tg_sync.downloaders import x as x_module
from tg_sync.downloaders.base import DownloadError
from tg_sync.downloaders.x import XDownloader, _collect_paths, _mp4_variants
from tg_sync.links import parse_link


def fake_download(url: str, dest: Path, referer: str | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"data")
    return dest


def recording_download():
    calls: list[str] = []

    def fake(url: str, dest: Path, referer: str | None = None) -> Path:
        calls.append(url)
        return fake_download(url, dest, referer)

    return fake, calls


def tweet_with(*media: dict, text: str = "hello") -> dict:
    return {"__typename": "Tweet", "text": text, "mediaDetails": list(media)}


def photo(url: str = "https://pbs.twimg.com/media/a.jpg") -> dict:
    return {"type": "photo", "media_url_https": url}


def video() -> dict:
    return {
        "type": "video",
        "video_info": {
            "variants": [
                {"content_type": "application/x-mpegURL", "url": "https://v/pl.m3u8"},
                {"content_type": "video/mp4", "bitrate": 832000, "url": "https://v/360.mp4"},
                {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://v/720.mp4"},
            ]
        },
    }


async def test_photo_tweet_downloads_original(tmp_path: Path, monkeypatch) -> None:
    downloader = XDownloader()
    monkeypatch.setattr(
        downloader, "_fetch_tweet", lambda tweet_id: tweet_with(photo(), photo())
    )
    monkeypatch.setattr(x_module, "_download_url", fake_download)
    monkeypatch.setattr(
        downloader, "_run_ytdlp", lambda url, workdir: pytest.fail("不应调用 yt-dlp")
    )

    items = await downloader.download(
        parse_link("https://x.com/NASA/status/123"), tmp_path
    )

    assert [item.path.name for item in items] == ["123_01.jpg", "123_02.jpg"]
    assert items[0].title == "hello"
    assert items[0].source_url == "https://x.com/NASA/status/123"


async def test_video_tweet_downloads_best_mp4(tmp_path: Path, monkeypatch) -> None:
    downloader = XDownloader()
    monkeypatch.setattr(downloader, "_fetch_tweet", lambda tweet_id: tweet_with(video()))
    fake, calls = recording_download()
    monkeypatch.setattr(x_module, "_download_url", fake)
    monkeypatch.setattr(
        downloader, "_run_ytdlp", lambda url, workdir: pytest.fail("不应调用 yt-dlp")
    )

    items = await downloader.download(
        parse_link("https://x.com/NASA/status/123"), tmp_path
    )

    assert [item.path.name for item in items] == ["123_01.mp4"]
    assert calls == ["https://v/720.mp4"]


async def test_video_falls_back_to_ytdlp(tmp_path: Path, monkeypatch) -> None:
    downloader = XDownloader()
    monkeypatch.setattr(downloader, "_fetch_tweet", lambda tweet_id: tweet_with(video()))

    def broken(url: str, dest: Path, referer: str | None = None) -> Path:
        raise OSError("blocked")

    monkeypatch.setattr(x_module, "_download_url", broken)
    produced = tmp_path / "001-abc.mp4"
    produced.write_bytes(b"video")
    monkeypatch.setattr(downloader, "_run_ytdlp", lambda url, workdir: [produced])

    items = await downloader.download(
        parse_link("https://x.com/NASA/status/123"), tmp_path
    )

    assert [item.path for item in items] == [produced]


async def test_video_and_photo_together(tmp_path: Path, monkeypatch) -> None:
    downloader = XDownloader()
    monkeypatch.setattr(
        downloader, "_fetch_tweet", lambda tweet_id: tweet_with(video(), photo())
    )
    monkeypatch.setattr(x_module, "_download_url", fake_download)

    items = await downloader.download(
        parse_link("https://x.com/NASA/status/123"), tmp_path
    )

    assert [item.path.name for item in items] == ["123_01.mp4", "123_02.jpg"]


async def test_size_limit_picks_smaller_variant(tmp_path: Path, monkeypatch) -> None:
    downloader = XDownloader(max_file_size=1000)
    monkeypatch.setattr(downloader, "_fetch_tweet", lambda tweet_id: tweet_with(video()))
    fake, calls = recording_download()
    monkeypatch.setattr(x_module, "_download_url", fake)
    sizes = {"https://v/720.mp4": 5000, "https://v/360.mp4": 900}
    monkeypatch.setattr(x_module, "_content_length", lambda url: sizes.get(url))

    items = await downloader.download(
        parse_link("https://x.com/NASA/status/123"), tmp_path
    )

    assert len(items) == 1
    assert calls == ["https://v/360.mp4"]


async def test_all_variants_too_big_falls_back_to_ytdlp(
    tmp_path: Path, monkeypatch
) -> None:
    downloader = XDownloader(max_file_size=100)
    monkeypatch.setattr(downloader, "_fetch_tweet", lambda tweet_id: tweet_with(video()))
    monkeypatch.setattr(
        x_module, "_download_url", lambda *args, **kwargs: pytest.fail("不应下载直链")
    )
    monkeypatch.setattr(x_module, "_content_length", lambda url: 5000)
    produced = tmp_path / "001-abc.mp4"
    produced.write_bytes(b"video")
    monkeypatch.setattr(downloader, "_run_ytdlp", lambda url, workdir: [produced])

    items = await downloader.download(
        parse_link("https://x.com/NASA/status/123"), tmp_path
    )

    assert [item.path for item in items] == [produced]


async def test_text_only_tweet_raises(tmp_path: Path, monkeypatch) -> None:
    downloader = XDownloader()
    monkeypatch.setattr(downloader, "_fetch_tweet", lambda tweet_id: tweet_with())
    monkeypatch.setattr(downloader, "_run_ytdlp", lambda url, workdir: [])

    with pytest.raises(DownloadError, match="不包含可下载的媒体"):
        await downloader.download(parse_link("https://x.com/NASA/status/123"), tmp_path)


async def test_unreadable_tweet_hints_cookies(tmp_path: Path, monkeypatch) -> None:
    downloader = XDownloader()
    monkeypatch.setattr(downloader, "_fetch_tweet", lambda tweet_id: None)
    monkeypatch.setattr(downloader, "_run_ytdlp", lambda url, workdir: [])

    with pytest.raises(DownloadError, match="TG_YTDLP_COOKIES"):
        await downloader.download(parse_link("https://x.com/NASA/status/123"), tmp_path)


def test_mp4_variants_sorted_by_bitrate() -> None:
    variants = _mp4_variants(video())
    assert [variant["url"] for variant in variants] == [
        "https://v/720.mp4",
        "https://v/360.mp4",
    ]


def test_mp4_variants_without_variants() -> None:
    assert _mp4_variants({"type": "video"}) == []


def test_collect_paths_handles_playlists(tmp_path: Path) -> None:
    first = tmp_path / "001.jpg"
    second = tmp_path / "002.jpg"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    (tmp_path / "003.part").write_bytes(b"c")

    info = {
        "_type": "playlist",
        "entries": [
            {"requested_downloads": [{"filepath": str(first)}]},
            {"filepath": str(second)},
            {"filepath": str(tmp_path / "003.part")},
            {"filepath": str(tmp_path / "missing.jpg")},
            None,
        ],
    }

    assert _collect_paths(info) == [first, second]


def test_collect_paths_deduplicates(tmp_path: Path) -> None:
    path = tmp_path / "001.mp4"
    path.write_bytes(b"a")
    info = {"filepath": str(path), "requested_downloads": [{"filepath": str(path)}]}
    assert _collect_paths(info) == [path]


# --------------------------------------------------------------------- 停滞看门狗


class FakeResponse:
    def __init__(self, chunks: list[bytes], content_length: int | None = None) -> None:
        self._chunks = list(chunks)
        self.headers = (
            {"Content-Length": str(content_length)} if content_length is not None else {}
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read1(self, size: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if len(chunk) > size:
            self._chunks.insert(0, chunk[size:])
            return chunk[:size]
        return chunk


def test_download_once_success(tmp_path: Path, monkeypatch) -> None:
    response = FakeResponse([b"a" * 1000] * 4, content_length=4000)
    monkeypatch.setattr(
        x_module.urllib.request, "urlopen", lambda *a, **k: response
    )

    path = x_module._download_once("https://v/x.mp4", tmp_path / "x.mp4")

    assert path.read_bytes() == b"a" * 4000


def test_download_once_aborts_slow_trickle(tmp_path: Path, monkeypatch) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr(x_module.time, "monotonic", lambda: clock["t"])

    class SlowResponse(FakeResponse):
        def read1(self, size: int) -> bytes:
            clock["t"] += 1.0
            return super().read1(size)

    response = SlowResponse([b"a" * 1024] * 100)
    monkeypatch.setattr(
        x_module.urllib.request, "urlopen", lambda *a, **k: response
    )

    with pytest.raises(x_module._Stalled, match="KiB/s"):
        x_module._download_once("https://v/x.mp4", tmp_path / "x.mp4")


def test_download_once_detects_truncated_file(tmp_path: Path, monkeypatch) -> None:
    response = FakeResponse([b"a" * 1000], content_length=5000)
    monkeypatch.setattr(
        x_module.urllib.request, "urlopen", lambda *a, **k: response
    )

    with pytest.raises(x_module._Stalled, match="不完整"):
        x_module._download_once("https://v/x.mp4", tmp_path / "x.mp4")


def test_download_url_retries_after_stall(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def flaky(url: str, dest: Path, referer: str | None = None) -> Path:
        calls.append(url)
        if len(calls) < 3:
            raise x_module._Stalled("slow")
        dest.write_bytes(b"ok")
        return dest

    monkeypatch.setattr(x_module, "_download_once", flaky)
    monkeypatch.setattr(x_module.time, "sleep", lambda seconds: None)

    path = x_module._download_url("https://v/x.mp4", tmp_path / "x.mp4")

    assert path.read_bytes() == b"ok"
    assert len(calls) == 3


def test_download_url_gives_up_after_retries(tmp_path: Path, monkeypatch) -> None:
    def always_stalled(url: str, dest: Path, referer: str | None = None) -> Path:
        raise x_module._Stalled("slow")

    monkeypatch.setattr(x_module, "_download_once", always_stalled)
    monkeypatch.setattr(x_module.time, "sleep", lambda seconds: None)

    with pytest.raises(DownloadError, match="多次停滞"):
        x_module._download_url("https://v/x.mp4", tmp_path / "x.mp4")
