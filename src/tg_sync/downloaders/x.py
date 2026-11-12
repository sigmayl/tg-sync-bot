"""X (Twitter) 媒体下载。

X 的媒体有两种来源，各有短板，所以这里做双通道：

1. 公共 syndication 接口（无需登录）能拿到一条推文的完整媒体清单：
   图片原图直链、视频的 mp4/m3u8 变体。纯图片推文只能走这条路。
2. yt-dlp 擅长视频（HLS 合并、重试、cookies），但它会跳过图片
   （extractor 里过滤了 type != 'photo'），纯图片推文会直接报错；
   而且它优先走 HLS 分片，实测比 mp4 直链慢一个数量级。

流程：syndication 探测媒体清单 → 视频优先下载最高码率 mp4 直链
（按上传上限挑选能放下的最高画质）→ 直链失败才回落到 yt-dlp →
图片按原图分辨率下载。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..links import Link
from ..models import MediaItem
from .base import DownloadError

log = logging.getLogger(__name__)

MIB = 1024 * 1024
SYNDICATION_ENDPOINT = "https://cdn.syndication.twimg.com/tweet-result"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = 60
_SKIP_SUFFIXES = {".part", ".ytdl", ".json", ".temp"}

# 直链下载的停滞保护：慢连接会被换掉重连，避免一条坏 TCP 连接拖死任务
_IDLE_TIMEOUT = 30          # 单次读取最多等多久
_WARMUP = 20                # 前 N 秒不判断平均速度
_MIN_RATE = 64 * 1024       # 低于 64 KiB/s 视为被限速
_DOWNLOAD_RETRIES = 3
_CHUNK = 256 * 1024


class _Stalled(DownloadError):
    """连接活着但几乎不传数据，需要换一条连接重试。"""


class XDownloader:
    name = "x"

    def __init__(
        self,
        *,
        cookies: Path | None = None,
        cookies_from_browser: str | None = None,
        proxy: str | None = None,
        max_file_size: int = 0,
    ) -> None:
        self.cookies = cookies
        self.cookies_from_browser = cookies_from_browser
        self.proxy = proxy
        self.max_file_size = max_file_size

    async def download(self, link: Link, workdir: Path) -> list[MediaItem]:
        if link.tweet_id is None:
            raise DownloadError("无法从链接中解析推文 ID")

        tweet = await asyncio.to_thread(self._fetch_tweet, link.tweet_id)
        media: list[dict] = list((tweet or {}).get("mediaDetails") or [])
        title = (tweet or {}).get("text") or None

        # 保留媒体在推文中的顺序，用序号命名，避免多个视频/图片互相覆盖
        photos = [
            (index, item)
            for index, item in enumerate(media, 1)
            if item.get("type") == "photo"
        ]
        videos = [
            (index, item)
            for index, item in enumerate(media, 1)
            if item.get("type") in {"video", "animated_gif"}
        ]

        items: list[MediaItem] = []
        if videos:
            items.extend(await self._download_videos(videos, link, workdir, title))
        elif not media:
            # 没有公开媒体清单（受保护 / 仅登录可见）：交给 yt-dlp（可用 cookies）
            items.extend(await self._ytdlp_items(link, workdir, title))

        if photos:
            items.extend(await self._download_photos(photos, link, workdir, title))

        if not items:
            raise DownloadError(self._failure_hint(tweet))
        return items

    # ------------------------------------------------------------------ 探测

    def _fetch_tweet(self, tweet_id: int) -> dict | None:
        """读取推文元数据；读取失败返回 None（交给 yt-dlp 再试一次）。"""
        url = f"{SYNDICATION_ENDPOINT}?id={tweet_id}&lang=en&token=0"
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("读取推文 %s 失败：%s", tweet_id, exc)
            return None
        if not isinstance(payload, dict) or payload.get("__typename") != "Tweet":
            log.info("推文 %s 无公开数据（可能已删除或受保护）", tweet_id)
            return None
        return payload

    # ------------------------------------------------------------------ 视频

    async def _download_videos(
        self,
        videos: list[tuple[int, dict]],
        link: Link,
        workdir: Path,
        title: str | None,
    ) -> list[MediaItem]:
        items: list[MediaItem] = []
        for index, media in videos:
            variant = await self._pick_variant(media)
            if variant is None:
                continue
            dest = workdir / f"{link.tweet_id}_{index:02d}.mp4"
            try:
                path = await asyncio.to_thread(
                    _download_url, variant["url"], dest, "https://x.com/"
                )
            except Exception as exc:
                log.warning("下载视频失败 %s：%s", variant["url"], exc)
                continue
            items.append(MediaItem(path, source_url=link.url, title=title))

        if items:
            return items

        # 直链不可用（或所有画质都超过体积上限）时退回 yt-dlp
        log.info("mp4 直链不可用，改用 yt-dlp：%s", link.url)
        return await self._ytdlp_items(link, workdir, title)

    async def _pick_variant(self, media: dict) -> dict | None:
        variants = _mp4_variants(media)
        if not variants:
            return None
        if not self.max_file_size:
            return variants[0]
        for variant in variants:
            size = await asyncio.to_thread(_content_length, variant["url"])
            if size is None or size <= self.max_file_size:
                return variant
            log.info(
                "视频 %.1f MiB 超过上传上限，尝试更低画质：%s",
                size / MIB,
                variant["url"],
            )
        return None

    async def _ytdlp_items(
        self, link: Link, workdir: Path, title: str | None
    ) -> list[MediaItem]:
        paths = await asyncio.to_thread(self._run_ytdlp, link.url, workdir)
        return [MediaItem(path, source_url=link.url, title=title) for path in paths]

    def _run_ytdlp(self, url: str, workdir: Path) -> list[Path]:
        try:
            import yt_dlp
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise DownloadError("缺少 yt-dlp，请执行 pip install yt-dlp") from exc

        options: dict[str, Any] = {
            "outtmpl": str(workdir / "%(autonumber)03d-%(id)s.%(ext)s"),
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "noplaylist": False,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "consoletitle": False,
            "color": "no_color",
            "retries": 3,
            "fragment_retries": 3,
            "concurrent_fragments": 4,
            "socket_timeout": 30,
            "throttledratelimit": _MIN_RATE,
            "windowsfilenames": True,
            "trim_file_name": 120,
            "overwrites": True,
            "logger": _YdlLogger(),
        }
        if self.max_file_size:
            options["max_filesize"] = self.max_file_size
        if self.cookies:
            options["cookiefile"] = str(self.cookies)
        if self.cookies_from_browser:
            options["cookiesfrombrowser"] = tuple(
                part.strip() for part in self.cookies_from_browser.split(":")
            )
        if self.proxy:
            options["proxy"] = self.proxy

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as exc:  # yt-dlp 抛出的异常类型很杂
            log.warning("yt-dlp 下载失败 %s：%s", url, exc)
            return []

        paths = _collect_paths(info)
        if not paths:
            log.warning("yt-dlp 没有产出文件：%s", url)
        return paths

    # ------------------------------------------------------------------ 图片

    async def _download_photos(
        self,
        photos: list[tuple[int, dict]],
        link: Link,
        workdir: Path,
        title: str | None,
    ) -> list[MediaItem]:
        items: list[MediaItem] = []
        for index, photo in photos:
            url = photo.get("media_url_https") or photo.get("url")
            if not url:
                continue
            suffix = Path(urlsplit(url).path).suffix or ".jpg"
            dest = workdir / f"{link.tweet_id}_{index:02d}{suffix}"
            path: Path | None = None
            # name=orig 拿原图；个别情况下会被拒绝，退回默认尺寸
            for candidate in (f"{url}?name=orig", url):
                try:
                    path = await asyncio.to_thread(
                        _download_url, candidate, dest, "https://x.com/"
                    )
                    break
                except Exception as exc:
                    log.debug("下载图片失败（尝试下一个地址）%s：%s", candidate, exc)
            if path is None:
                log.warning("跳过图片 %s", url)
                continue
            items.append(MediaItem(path, source_url=link.url, title=title))
        return items

    @staticmethod
    def _failure_hint(tweet: dict | None) -> str:
        if tweet is None:
            return (
                "无法读取该推文（可能已删除、受保护或需要登录）。"
                "可在 .env 中配置 TG_YTDLP_COOKIES 或 "
                "TG_YTDLP_COOKIES_FROM_BROWSER 后重试"
            )
        return "该推文不包含可下载的媒体"


class _YdlLogger:
    """把 yt-dlp 的日志接到标准 logging。"""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.error("yt-dlp: %s", msg)


def _mp4_variants(media: dict) -> list[dict]:
    """按码率从高到低返回可用的 mp4 变体。"""
    variants = [
        variant
        for variant in (media.get("video_info") or {}).get("variants", [])
        if variant.get("content_type") == "video/mp4" and variant.get("url")
    ]
    return sorted(variants, key=lambda variant: variant.get("bitrate") or 0, reverse=True)


def _content_length(url: str) -> int | None:
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.headers.get("Content-Length")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.debug("读取文件大小失败 %s：%s", url, exc)
        return None
    return int(raw) if raw and str(raw).isdigit() else None


def _download_url(url: str, dest: Path, referer: str | None = None) -> Path:
    """下载直链；遇到「连接活着但几乎不传数据」时换连接重试。"""
    last_error: Exception | None = None
    for attempt in range(1, _DOWNLOAD_RETRIES + 1):
        try:
            return _download_once(url, dest, referer)
        except _Stalled as exc:
            last_error = exc
            log.warning(
                "下载停滞（第 %s/%s 次，将重连）：%s",
                attempt,
                _DOWNLOAD_RETRIES,
                exc,
            )
            if attempt < _DOWNLOAD_RETRIES:
                time.sleep(min(2**attempt, 5))
    raise DownloadError(f"下载失败（多次停滞）：{last_error}")


def _download_once(url: str, dest: Path, referer: str | None = None) -> Path:
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    dest.parent.mkdir(parents=True, exist_ok=True)
    expected = 0

    with urllib.request.urlopen(request, timeout=_IDLE_TIMEOUT) as response, open(
        dest, "wb"
    ) as handle:
        raw_length = response.headers.get("Content-Length")
        if raw_length and str(raw_length).isdigit():
            expected = int(raw_length)
        start = time.monotonic()
        total = 0
        while True:
            try:
                # read1 只做一次底层读取，有多少返回多少；
                # read/readinto 会一直阻塞到填满缓冲区，看门狗就没机会跑了。
                chunk = response.read1(_CHUNK)
            except TimeoutError as exc:  # socket.timeout 在 3.10+ 是 TimeoutError
                raise _Stalled(f"{_IDLE_TIMEOUT}s 内没有收到数据") from exc
            if not chunk:
                break
            handle.write(chunk)
            total += len(chunk)
            elapsed = time.monotonic() - start
            if elapsed > _WARMUP and total / elapsed < _MIN_RATE:
                raise _Stalled(
                    f"平均 {total / elapsed / 1024:.0f} KiB/s，"
                    f"低于 {_MIN_RATE // 1024} KiB/s"
                )

    if expected and total != expected:
        raise _Stalled(f"文件不完整：{total}/{expected} 字节")
    if dest.stat().st_size == 0:
        raise DownloadError(f"下载到的文件为空：{url}")
    return dest


def _collect_paths(info: Any) -> list[Path]:
    """从 yt-dlp 的 info 字典里收集已下载的文件路径。"""
    found: list[Path] = []
    seen: set[Path] = set()

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("_type") == "playlist":
            for entry in node.get("entries") or []:
                walk(entry)
            return

        candidates: list[str | None] = [
            download.get("filepath")
            for download in node.get("requested_downloads") or []
            if isinstance(download, dict)
        ]
        candidates += [node.get("filepath"), node.get("_filename")]

        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if path in seen or path.suffix.lower() in _SKIP_SUFFIXES:
                continue
            if not path.exists():
                continue
            seen.add(path)
            found.append(path)

    walk(info)
    return found
