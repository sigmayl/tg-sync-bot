"""链接解析：把用户粘贴的 URL 归一化成可下载的目标。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit


class LinkError(ValueError):
    """链接无法识别或暂不支持。"""


class LinkKind(str, Enum):
    X = "x"
    TELEGRAM = "telegram"


# 归一化后（去掉 www.）的主机名
X_HOSTS = {
    "x.com",
    "mobile.x.com",
    "twitter.com",
    "mobile.twitter.com",
    "vxtwitter.com",
    "fxtwitter.com",
    "fixupx.com",
    "fixvx.com",
}
TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.dog"}

_X_STATUS_RE = re.compile(
    r"^/(?:(?P<user>[A-Za-z0-9_]{1,15})/|i/web/)status(?:es)?/(?P<tweet>\d+)"
)
_TG_PRIVATE_RE = re.compile(r"^/c/(?P<channel>\d+)/(?P<message>\d+)$")
_TG_PUBLIC_RE = re.compile(
    r"^/(?:s/)?(?P<user>[A-Za-z0-9_]{3,32})/(?P<message>\d+)$"
)
_TG_CHANNEL_RE = re.compile(r"^/(?:s/)?(?P<user>[A-Za-z0-9_]{3,32})/?$")

# 中文标点也一并去掉，方便直接粘贴
_TRAILING_JUNK = ".,;:!?)]}>'\"。，；：！？、）】》」』"


@dataclass(frozen=True, slots=True)
class Link:
    kind: LinkKind
    url: str
    raw: str
    username: str | None = None
    channel_id: int | None = None
    message_id: int | None = None
    tweet_id: int | None = None

    def describe(self) -> str:
        if self.kind is LinkKind.X:
            return f"X 推文 {self.tweet_id}"
        if self.channel_id is not None:
            return f"Telegram 私有频道消息 {self.message_id}"
        return f"Telegram @{self.username} 的消息 {self.message_id}"


def parse_link(raw: str) -> Link:
    """解析并归一化链接，失败抛 LinkError。"""
    text = (raw or "").strip().strip("<>").rstrip(_TRAILING_JUNK)
    if not text:
        raise LinkError("链接为空")

    candidate = text if "://" in text else f"https://{text}"
    parts = urlsplit(candidate)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", parts.path or "/")

    if host in X_HOSTS:
        return _parse_x(text, path)
    if host in TELEGRAM_HOSTS:
        return _parse_telegram(text, path)
    raise LinkError(f"暂不支持该站点：{host or text}（目前支持 X 和 t.me）")


def _parse_x(raw: str, path: str) -> Link:
    match = _X_STATUS_RE.match(path)
    if not match:
        if re.match(r"^/[A-Za-z0-9_]{1,15}/?$", path):
            raise LinkError("这是 X 的主页链接，请提供具体推文（…/status/数字）")
        raise LinkError("无法识别的 X 链接，请提供 …/status/数字 形式的推文链接")

    tweet_id = int(match.group("tweet"))
    username = match.group("user")
    normalized = (
        f"https://x.com/{username}/status/{tweet_id}"
        if username
        else f"https://x.com/i/status/{tweet_id}"
    )
    return Link(
        kind=LinkKind.X,
        url=normalized,
        raw=raw,
        username=username,
        tweet_id=tweet_id,
    )


def _parse_telegram(raw: str, path: str) -> Link:
    if path.startswith("/+") or path.startswith("/joinchat/"):
        raise LinkError("不支持群组/频道邀请链接，请提供具体消息链接")
    if path.startswith(("/addstickers/", "/share/", "/proxy", "/socks")):
        raise LinkError("不支持该类型的 t.me 链接，请提供具体消息链接")

    private = _TG_PRIVATE_RE.match(path)
    if private:
        return Link(
            kind=LinkKind.TELEGRAM,
            url=f"https://t.me/c/{private.group('channel')}/{private.group('message')}",
            raw=raw,
            channel_id=int(f"-100{private.group('channel')}"),
            message_id=int(private.group("message")),
        )

    public = _TG_PUBLIC_RE.match(path)
    if public:
        return Link(
            kind=LinkKind.TELEGRAM,
            url=f"https://t.me/{public.group('user')}/{public.group('message')}",
            raw=raw,
            username=public.group("user"),
            message_id=int(public.group("message")),
        )

    channel = _TG_CHANNEL_RE.match(path)
    if channel:
        raise LinkError(
            f"这是频道/用户主页（@{channel.group('user')}），请提供具体消息链接，"
            "例如 https://t.me/name/123"
        )

    raise LinkError("无法识别的 t.me 链接，请提供 …/频道名/消息ID 形式的链接")
