"""视频元数据与封面提取（依赖系统 ffprobe / ffmpeg）。

Telegram 不会在服务端给视频生成封面，时长/分辨率也要客户端通过
``DocumentAttributeVideo`` 显式告诉它。Telethon 只有在装了 mutagen 时才会
自己读这些信息，否则会退化成 ``duration=0, w=1, h=1``——表现就是
「没有封面、不显示时长，点开播放后才出现」。
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_PROBE_TIMEOUT = 30
_FFMPEG_TIMEOUT = 60
_THUMB_BOX = 320


@dataclass(slots=True)
class VideoMeta:
    duration: int
    width: int
    height: int


def probe_video(path: Path) -> VideoMeta | None:
    """读取视频时长与分辨率；失败返回 None（不影响上传）。"""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration:stream_tags=rotate:stream_side_data=rotation",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("ffprobe 不可用或超时：%s", exc)
        return None
    if proc.returncode != 0:
        log.debug("ffprobe 失败：%s", proc.stderr.strip()[:200])
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return None

    streams = data.get("streams") or []
    if not streams:
        return None
    stream = streams[0]
    duration = _as_float(stream.get("duration")) or _as_float(
        (data.get("format") or {}).get("duration")
    )
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if not duration or width <= 0 or height <= 0:
        return None

    # 竖屏视频常见「编码尺寸横向 + rotation=90」，属性要按显示尺寸给
    if abs(_rotation(stream)) % 180 == 90:
        width, height = height, width
    return VideoMeta(duration=max(1, round(duration)), width=width, height=height)


def extract_thumbnail(path: Path, dest: Path) -> Path | None:
    """抽一帧作为封面（≤320×320 的 JPEG）；失败返回 None。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for seek in ("0.5", "0"):
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            seek,
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={_THUMB_BOX}:{_THUMB_BOX}:force_original_aspect_ratio=decrease",
            "-q:v",
            "5",
            str(dest),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("ffmpeg 不可用或超时：%s", exc)
            return None
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return dest
        log.debug("ffmpeg 截帧失败（seek=%s）：%s", seek, proc.stderr.strip()[:200])
    return None


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            return round(_as_float(side["rotation"]))
    raw = (stream.get("tags") or {}).get("rotate")
    return round(_as_float(raw))
