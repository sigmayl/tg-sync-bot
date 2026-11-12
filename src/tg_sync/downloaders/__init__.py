"""下载器。"""

from __future__ import annotations

from .base import Downloader, DownloadError
from .telegram import TelegramDownloader
from .x import XDownloader

__all__ = ["DownloadError", "Downloader", "TelegramDownloader", "XDownloader"]
