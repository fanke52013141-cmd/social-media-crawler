# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests/test_media_ffmpeg.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

"""ffmpeg 音视频合流测试。

本机没装 ffmpeg 时整体跳过（不影响其他测试）。素材用 ffmpeg 内置的 lavfi 源生成，
不依赖任何外部文件。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from media_downloader import merge_audio_video
from media_downloader.ffmpeg import ffmpeg_path, is_available
from media_downloader.types import MediaDownloadError

pytestmark = pytest.mark.skipif(not is_available(), reason="本机未安装 ffmpeg")


def _ffprobe_path() -> str:
    probe = shutil.which("ffprobe")
    if not probe:
        pytest.skip("本机未安装 ffprobe")
    return probe


def _make_video(path: Path, seconds: int = 1) -> None:
    # 显式 -f mp4：.m4s 不是 ffmpeg 能自动识别的容器后缀，需模拟 DASH 分段的真实形态
    subprocess.run(
        [
            ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=red:s=320x240:d={seconds}",
            "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-f", "mp4", str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_audio(path: Path, seconds: int = 1) -> None:
    subprocess.run(
        [
            ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
            "-c:a", "aac", "-f", "mp4", str(path),
        ],
        check=True,
        capture_output=True,
    )


def _stream_types(path: Path) -> list[str]:
    result = subprocess.run(
        [
            _ffprobe_path(), "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_merge_produces_playable_file_with_both_streams(tmp_path):
    video = tmp_path / "video.m4s"
    audio = tmp_path / "audio.m4s"
    merged = tmp_path / "merged.mp4"
    _make_video(video)
    _make_audio(audio)

    await merge_audio_video(video, audio, merged)

    assert merged.exists()
    assert merged.stat().st_size > 0
    assert _stream_types(merged) == ["video", "audio"]


@pytest.mark.asyncio
async def test_merge_rejects_corrupt_input(tmp_path):
    video = tmp_path / "video.m4s"
    bad_audio = tmp_path / "audio.m4s"
    merged = tmp_path / "merged.mp4"
    _make_video(video)
    bad_audio.write_bytes(b"this is definitely not an audio stream")

    with pytest.raises(MediaDownloadError):
        await merge_audio_video(video, bad_audio, merged)


def _pid_alive(pid: int) -> bool:
    """跨平台判断进程是否仍存活（Windows 不能用 os.kill(pid, 0)，那会真的终止进程）。"""
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@pytest.mark.asyncio
async def test_merge_kills_subprocess_when_cancelled(tmp_path, monkeypatch):
    """任务被取消（Ctrl-C / abort）时不能留下孤儿 ffmpeg 进程。

    跨平台实现：把 merge 实际拉起的子进程替换为「Python 解释器 + 睡眠脚本」，
    脚本先把自己的 PID 写入文件再长睡；取消后按 PID 校验进程确实被回收。
    （旧实现用 sh 脚本 + pgrep，仅 POSIX 可用，Windows 必挂。）
    """
    import media_downloader.ffmpeg as ffmpeg_module

    pid_file = tmp_path / "fake_ffmpeg.pid"
    sleeper = tmp_path / "fake_ffmpeg_sleeper.py"
    sleeper.write_text(
        "import os, time\n"
        f"open(r'{pid_file}', 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    real_create = asyncio.create_subprocess_exec

    async def fake_create(executable, *args, **kwargs):
        # 无论模块传什么 ffmpeg 参数，实际运行睡眠脚本（参数被脚本忽略）
        return await real_create(sys.executable, str(sleeper), **kwargs)

    monkeypatch.setattr(ffmpeg_module, "ffmpeg_path", lambda: sys.executable)
    monkeypatch.setattr(ffmpeg_module.asyncio, "create_subprocess_exec", fake_create)

    task = asyncio.create_task(
        ffmpeg_module.merge_audio_video(
            tmp_path / "v.m4s", tmp_path / "a.m4s", tmp_path / "o.mp4", timeout=60
        )
    )
    for _ in range(50):
        if pid_file.exists():
            break
        await asyncio.sleep(0.1)
    assert pid_file.exists(), "假 ffmpeg 子进程未启动"
    pid = int(pid_file.read_text().strip())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)

    assert not _pid_alive(pid), f"取消后子进程仍在运行: {pid}"


@pytest.mark.asyncio
async def test_merge_reports_missing_ffmpeg(tmp_path, monkeypatch):
    import media_downloader.ffmpeg as ffmpeg_module

    monkeypatch.setattr(ffmpeg_module, "ffmpeg_path", lambda: None)
    video = tmp_path / "video.m4s"
    audio = tmp_path / "audio.m4s"
    video.write_bytes(b"x")
    audio.write_bytes(b"y")

    with pytest.raises(MediaDownloadError, match="ffmpeg"):
        await merge_audio_video(video, audio, tmp_path / "out.mp4")


def test_is_available_matches_executable():
    assert is_available() is bool(ffmpeg_path())
