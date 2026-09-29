"""Probe source recordings and build one continuous PCM course recording."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from local_tools import find_executable


def probe_audio(path: Path) -> float:
    ffprobe = find_executable("ffprobe")
    if not ffprobe:
        raise RuntimeError("找不到 ffprobe，無法檢查錄音。")
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("無法檢查錄音格式或長度。") from exc
    if result.returncode != 0:
        raise ValueError("錄音格式無法讀取，請確認檔案未損壞。")
    try:
        payload = json.loads(result.stdout)
        has_audio = any(stream.get("codec_type") == "audio" for stream in payload.get("streams", []))
        duration = float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("無法判斷錄音長度。") from exc
    if not has_audio or not math.isfinite(duration) or not 0 < duration <= 24 * 3600:
        raise ValueError("錄音必須含音軌，且長度需介於 0 到 24 小時。")
    return duration


def merge_audio(parts: list[Path], destination: Path) -> float:
    """Decode mixed input formats in order into a seekable 16 kHz mono WAV."""
    if not parts or len(parts) > 32:
        raise ValueError("每堂課需有 1 到 32 段錄音。")
    ffmpeg = find_executable("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，無法合併錄音。")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    for part in parts:
        command.extend(["-i", str(part)])
    filters = [
        f"[{index}:a:0]aresample=16000,aformat=sample_fmts=s16:channel_layouts=mono,"
        f"asetpts=PTS-STARTPTS[a{index}]"
        for index in range(len(parts))
    ]
    filters.append("".join(f"[a{index}]" for index in range(len(parts))) +
                   f"concat=n={len(parts)}:v=0:a=1[out]")
    command.extend(["-filter_complex", ";".join(filters), "-map", "[out]", "-vn",
                    "-c:a", "pcm_s16le", "-rf64", "auto", str(destination)])
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError("無法啟動 ffmpeg 合併錄音。") from exc
    if result.returncode != 0 or not destination.is_file():
        detail = (result.stderr or "").strip()[-1200:]
        raise RuntimeError("ffmpeg 無法合併錄音。" + (f"\n{detail}" if detail else ""))
    duration = probe_audio(destination)
    if destination.stat().st_size > 4 * 1024 * 1024 * 1024:
        raise ValueError("合併後 WAV 超過 4 GB，請縮短課程錄音。")
    return duration
