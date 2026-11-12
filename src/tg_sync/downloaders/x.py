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
import http.cookiejar
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

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
# 网页端公开的 Bearer token（写死在前端 JS 里，非用户凭据）
WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
GRAPHQL_ORIGIN = "https://x.com"
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
        self._query_id: str | None = None

    async def download(self, link: Link, workdir: Path) -> list[MediaItem]:
        if link.tweet_id is None:
            raise DownloadError("无法从链接中解析推文 ID")

        tweet = await asyncio.to_thread(self._fetch_tweet, link.tweet_id)
        media: list[dict] = list((tweet or {}).get("mediaDetails") or [])
        title = (tweet or {}).get("text") or None

        # syndication 是游客通道，对敏感/成人内容一律返回 TweetTombstone。
        # 有 cookies 时改用登录态 GraphQL 再取一次，避免把「需要登录」误判成「已删除」。
        if not media and self.cookies:
            logged_in = await asyncio.to_thread(self._fetch_tweet_graphql, link.tweet_id)
            if logged_in is not None:
                tweet = logged_in
                media = list(logged_in.get("mediaDetails") or [])
                title = logged_in.get("text") or title

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

    # ------------------------------------------------------- 登录态 GraphQL

    def _load_cookies(self) -> dict[str, str]:
        """从 Netscape cookies 文件里取出 auth_token / ct0。"""
        if not self.cookies:
            return {}
        try:
            jar = http.cookiejar.MozillaCookieJar(str(self.cookies))
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError) as exc:
            log.warning("读取 cookies 失败 %s：%s", self.cookies, exc)
            return {}
        return {cookie.name: cookie.value for cookie in jar}

    def _fetch_tweet_graphql(self, tweet_id: int) -> dict | None:
        """用登录态调 TweetDetail，取回 syndication 看不到的推文（敏感内容等）。

        返回与 syndication 兼容的字典（mediaDetails / text），失败返回 None。
        """
        cookies = self._load_cookies()
        auth_token = cookies.get("auth_token")
        ct0 = cookies.get("ct0")
        if not auth_token or not ct0:
            log.info("cookies 缺少 auth_token / ct0，跳过登录态读取")
            return None

        query_id = self._tweet_detail_query_id()
        if query_id is None:
            return None

        variables = {
            "focalTweetId": str(tweet_id),
            "with_rux_injections": False,
            "includePromotedContent": True,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
            "withV2Timeline": True,
        }
        features = {
            "rweb_video_screen_enabled": False,
            "profile_label_improvements_pcf_label_in_post_enabled": True,
            "rweb_tipjar_consumption_enabled": True,
            "verified_phone_label_enabled": False,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "premium_content_api_read_enabled": False,
            "communities_web_enable_tweet_community_results_fetch": True,
            "c9s_tweet_anatomy_moderator_badge_enabled": True,
            "articles_preview_enabled": True,
            "responsive_web_edit_tweet_api_enabled": True,
            "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
            "view_counts_everywhere_api_enabled": True,
            "longform_notetweets_consumption_enabled": True,
            "responsive_web_twitter_article_tweet_consumption_enabled": True,
            "tweet_awards_web_tipping_enabled": False,
            "freedom_of_speech_not_reach_fetch_enabled": True,
            "standardized_nudges_misinfo": True,
            "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
            "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": True,
            "responsive_web_enhance_cards_enabled": False,
        }
        url = (
            f"{GRAPHQL_ORIGIN}/i/api/graphql/{query_id}/TweetDetail"
            f"?variables={quote(json.dumps(variables))}"
            f"&features={quote(json.dumps(features))}"
        )
        headers = {
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {WEB_BEARER}",
            "Cookie": f"auth_token={auth_token}; ct0={ct0}",
            "x-csrf-token": ct0,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "Accept": "*/*",
            "Referer": f"https://x.com/i/status/{tweet_id}",
            "content-type": "application/json",
        }
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("登录态读取推文 %s 失败：%s", tweet_id, exc)
            return None

        node = _find_tweet_node(payload, str(tweet_id))
        if node is None:
            log.info("登录态也读不到推文 %s（可能已删除）", tweet_id)
            return None

        legacy = node.get("legacy") or {}
        entities = legacy.get("extended_entities") or legacy.get("entities") or {}
        media_details: list[dict] = []
        for item in entities.get("media") or []:
            detail: dict = {
                "type": item.get("type"),
                "media_url_https": item.get("media_url_https"),
                "video_info": item.get("video_info"),
            }
            media_details.append(detail)
        return {"mediaDetails": media_details, "text": legacy.get("full_text") or None}

    def _tweet_detail_query_id(self) -> str | None:
        """从网页 bundle 里抓 TweetDetail 的 queryId（X 会不定期更换）。"""
        if self._query_id is not None:
            return self._query_id or None
        self._query_id = ""

        cookies = self._load_cookies()
        auth_token = cookies.get("auth_token")
        ct0 = cookies.get("ct0")
        cookie_header = f"auth_token={auth_token}; ct0={ct0}" if auth_token and ct0 else ""

        page = ""
        # 带 cookie 的推文页才会下发 bundle 引用；用公开推文做探针，不受目标推文状态影响
        for probe in ("https://x.com/NASA/status/2119083432835764635", "https://x.com/NASA"):
            headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
            if cookie_header:
                headers["Cookie"] = cookie_header
            try:
                request = urllib.request.Request(probe, headers=headers)
                with urllib.request.urlopen(request, timeout=20) as response:
                    page = response.read().decode("utf-8", "replace")
                if page:
                    break
            except (urllib.error.URLError, OSError) as exc:
                log.debug("探测 %s 失败：%s", probe, exc)
        if not page:
            log.warning("读取 X 页面失败，无法获取 TweetDetail queryId")
            return None

        bundles = sorted(
            set(re.findall(r"https://abs\.twimg\.com/responsive-web/client-web[^\"'\\ ]+?\.js", page))
        )
        source = page
        for bundle in bundles:
            try:
                req = urllib.request.Request(bundle, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=20) as response:
                    source += response.read().decode("utf-8", "replace")
            except (urllib.error.URLError, OSError):
                continue

        match = re.search(
            r'queryId:"([A-Za-z0-9_-]{20,})",operationName:"TweetDetail"', source
        )
        if match is None:
            log.warning("未能从 bundle 中解析 TweetDetail queryId")
            return None
        self._query_id = match.group(1)
        log.debug("TweetDetail queryId = %s", self._query_id)
        return self._query_id

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
        expected = str(link.tweet_id) if link.tweet_id is not None else None
        paths = await asyncio.to_thread(self._run_ytdlp, link.url, workdir, expected)
        return [MediaItem(path, source_url=link.url, title=title) for path in paths]

    def _run_ytdlp(self, url: str, workdir: Path, expected_id: str | None = None) -> list[Path]:
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

        # yt-dlp 的 X extractor 在目标推文不可用时会顺着页面推荐跳到同账号
        # 另一条推文，静默产出错误的文件。这里校验 id，不一致就丢弃。
        actual_id = str((info or {}).get("id") or "")
        if expected_id and actual_id and actual_id != expected_id:
            log.warning(
                "yt-dlp 返回了另一条推文（期望 %s，实际 %s），已丢弃：%s",
                expected_id,
                actual_id,
                url,
            )
            for path in _collect_paths(info):
                path.unlink(missing_ok=True)
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


def _find_tweet_node(payload: Any, tweet_id: str) -> dict | None:
    """在 GraphQL 响应里找到指定 id 的推文节点。"""
    found: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("rest_id") == tweet_id and isinstance(node.get("legacy"), dict):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found[0] if found else None


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
