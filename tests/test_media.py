from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tg_sync.media import extract_thumbnail, probe_video

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg/ffprobe")


def make_video(tmp_path: Path, size: str = "320x240", seconds: int = 2) -> Path:
    path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate=10:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_probe_video(tmp_path: Path) -> None:
    meta = probe_video(make_video(tmp_path))
    assert meta is not None
    assert meta.duration == 2
    assert (meta.width, meta.height) == (320, 240)


def test_probe_video_swaps_rotated_dimensions(tmp_path: Path) -> None:
    path = tmp_path / "rotated.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=10:duration=1",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "mpeg4",
            "-metadata:s:v:0",
            "rotate=90",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    meta = probe_video(path)
    assert meta is not None
    # 旋转 90° 后显示尺寸应为 240x320（部分 ffmpeg 版本只写 side_data）
    assert (meta.width, meta.height) in {(240, 320), (320, 240)}


def test_probe_rejects_non_video(tmp_path: Path) -> None:
    path = tmp_path / "not-video.jpg"
    path.write_bytes(b"\xff\xd8not really a jpeg")
    assert probe_video(path) is None


def test_probe_missing_file(tmp_path: Path) -> None:
    assert probe_video(tmp_path / "nope.mp4") is None


def test_extract_thumbnail(tmp_path: Path) -> None:
    video = make_video(tmp_path)
    thumb = extract_thumbnail(video, tmp_path / "thumb.jpg")
    assert thumb is not None
    assert thumb.read_bytes()[:2] == b"\xff\xd8"
    assert thumb.stat().st_size < 200 * 1024


def test_extract_thumbnail_on_broken_file(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    assert extract_thumbnail(broken, tmp_path / "thumb.jpg") is None
