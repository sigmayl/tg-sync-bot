"""命令解析：/sync <url> [<url> ...]"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_URLS = 10

SYNC_COMMAND = "sync"

_COMMAND_RE = re.compile(
    r"^/(?P<name>[A-Za-z0-9_]+)(?:@(?P<bot>[A-Za-z0-9_]+))?(?:\s+(?P<args>.*))?$",
    re.DOTALL,
)


class CommandError(ValueError):
    """命令格式错误（会回复给用户）。"""


FORCE_DOWNLOAD_FLAGS = {"--download", "-d"}


@dataclass(frozen=True, slots=True)
class SyncCommand:
    urls: tuple[str, ...]
    text: str
    force_download: bool = False


def command_name(text: str | None) -> str | None:
    """取出命令名（去掉 / 和 @botname），不是命令返回 None。"""
    if not text:
        return None
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None
    token = stripped.split(maxsplit=1)[0]
    return token[1:].split("@", 1)[0].lower() or None


def parse_command(text: str | None) -> SyncCommand | None:
    """解析 /sync 命令。

    返回 None 表示这条消息不是 /sync 命令（直接忽略）；
    抛 CommandError 表示是 /sync 但参数有问题（需要提示用户）。
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    match = _COMMAND_RE.match(stripped)
    if not match or match.group("name").lower() != SYNC_COMMAND:
        return None

    args = (match.group("args") or "").strip()
    urls: list[str] = []
    force_download = False
    for token in args.split():
        if token in FORCE_DOWNLOAD_FLAGS:
            force_download = True
        elif token.startswith("-"):
            raise CommandError(f"未知参数：{token}")
        else:
            urls.append(token)

    if not urls:
        raise CommandError(
            "用法：/sync [--download] <链接>\n"
            "例如：/sync https://x.com/NASA/status/1234567890\n"
            "  --download：t.me 链接强制下载后上传（默认优先服务端转发）"
        )
    if len(urls) > MAX_URLS:
        raise CommandError(f"一次最多处理 {MAX_URLS} 个链接，当前 {len(urls)} 个")

    return SyncCommand(
        urls=tuple(urls), text=stripped, force_download=force_download
    )
