"""下载产物的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav", ".aac"}


@dataclass(slots=True)
class MediaItem:
    """一个已落盘的媒体文件。"""

    path: Path
    source_url: str | None = None
    title: str | None = None
    size: int = 0

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if not self.size:
            try:
                self.size = self.path.stat().st_size
            except OSError:
                self.size = 0

    @property
    def kind(self) -> str:
        suffix = self.path.suffix.lower()
        if suffix in PHOTO_SUFFIXES:
            return "photo"
        if suffix in VIDEO_SUFFIXES:
            return "video"
        if suffix in AUDIO_SUFFIXES:
            return "audio"
        return "document"

    @property
    def display_name(self) -> str:
        return self.path.name
