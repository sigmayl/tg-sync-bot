"""下载器公共接口。"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..links import Link
from ..models import MediaItem


class DownloadError(RuntimeError):
    """下载失败（消息会直接展示给用户）。"""


@runtime_checkable
class Downloader(Protocol):
    name: str

    async def download(self, link: Link, workdir: Path) -> list[MediaItem]:
        """把 link 指向的媒体下载到 workdir，返回已落盘的文件。"""
        ...
