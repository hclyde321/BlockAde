"""Local audio transcription using ffmpeg and whisper.cpp's whisper-cli."""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import db
from local_tools import find_executable
import transcription_quality as quality
from text_conversion import to_traditional_verbatim


_DIAGNOSTIC_TAIL_BYTES = 8 * 1024
_OUTPUT_LINE_BYTES = 64 * 1024
DEFAULT_MODEL = "models/ggml-large-v3.bin"
VERBATIM_DECODING_ARGS = ["-bs", "5", "-bo", "5", "-tp", "0", "-tpi", "0.2"]
_WHISPER_PROGRESS_RE = re.compile(r"\bprogress\s*=\s*(\d{1,3}(?:\.\d+)?)\s*%", re.IGNORECASE)
_WHISPER_TIMESTAMP_RE = re.compile(
    r"(?:\[|\b)(\d{2}:\d{2}:\d{2}[,.]\d{1,6})\s*-->\s*"
    r"(\d{2}:\d{2}:\d{2}[,.]\d{1,6})(?:\]|\b)"
)


def _notify_progress(
    callback: Callable[[float, str], None] | None,
    fraction: float,
    message: str,
) -> None:
    if callback is not None:
        callback(max(0.0, min(1.0, float(fraction))), message)


def _append_bounded(buffer: bytearray, chunk: bytes, limit: int = _DIAGNOSTIC_TAIL_BYTES) -> None:
    buffer.extend(chunk)
    if len(buffer) > limit:
        del buffer[:-limit]


def _run_streaming(
    command: list[str],
    *,
    timeout: float | None,
    line_callback: Callable[[str, str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a child while consuming both pipes and keeping only bounded diagnostics."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    streams = {
        process.stdout.fileno(): ("stdout", process.stdout, bytearray()),
        process.stderr.fileno(): ("stderr", process.stderr, bytearray()),
    }
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    for fd, stream_data in streams.items():
        selector.register(fd, selectors.EVENT_READ, stream_data)

    started = time.monotonic()

    def emit_complete_lines(pending: bytearray, name: str, final: bool = False) -> None:
        while pending:
            newline_positions = [position for position in (pending.find(b"\n"), pending.find(b"\r")) if position >= 0]
            if not newline_positions:
                break
            end = min(newline_positions)
            line = bytes(pending[:end])
            delimiter = pending[end]
            del pending[:end + 1]
            if delimiter == 13 and pending[:1] == b"\n":
                del pending[:1]
            if line_callback is not None and line:
                line_callback(name, line.decode("utf-8", errors="replace"))
        if final and pending:
            line = bytes(pending)
            pending.clear()
            if line_callback is not None:
                line_callback(name, line.decode("utf-8", errors="replace"))
        elif len(pending) > _OUTPUT_LINE_BYTES:
            # A malformed or unusually long line must not defeat bounded memory use.
            del pending[:-_OUTPUT_LINE_BYTES]

    def timeout_error() -> subprocess.TimeoutExpired:
        return subprocess.TimeoutExpired(
            command,
            timeout,
            output=tails["stdout"].decode("utf-8", errors="replace"),
            stderr=tails["stderr"].decode("utf-8", errors="replace"),
        )

    try:
        while selector.get_map():
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                raise timeout_error()
            wait_for = None if remaining is None else min(remaining, 0.5)
            for key, _ in selector.select(wait_for):
                name, stream, pending = key.data
                chunk = os.read(key.fd, 4096)
                if not chunk:
                    emit_complete_lines(pending, name, final=True)
                    selector.unregister(key.fd)
                    stream.close()
                    continue
                _append_bounded(tails[name], chunk)
                pending.extend(chunk)
                emit_complete_lines(pending, name)

        remaining = None if timeout is None else max(0.0, timeout - (time.monotonic() - started))
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise timeout_error() from exc
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if not stream.closed:
                stream.close()

    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout=tails["stdout"].decode("utf-8", errors="replace"),
        stderr=tails["stderr"].decode("utf-8", errors="replace"),
    )


def _tool(name: str) -> str:
    path = find_executable(name)
    if not path:
        raise RuntimeError(f"找不到 {name}。請安裝後重新啟動本機服務。")
    return path


def _duration_seconds(audio_path: Path) -> float | None:
    try:
        ffprobe = _tool("ffprobe")
    except RuntimeError:
        return None
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        text=True, capture_output=True, check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return max(0.0, float(result.stdout.strip()))
    except ValueError:
        return None


def _timestamp_ms(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        # Standard Whisper segment start/end values are seconds.
        return round(float(value) * 1000)
    if not isinstance(value, str):
        return None
    match = re.search(r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,6})", value)
    if not match:
        try:
            return round(float(value) * 1000)
        except ValueError:
            return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    millis = int(match.group(4).ljust(3, "0")[:3])
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _pick_time(segment: dict[str, Any], edge: str) -> int | None:
    offset = segment.get("offsets", {}).get(edge) if isinstance(segment.get("offsets"), dict) else None
    if offset is not None:
        # whisper.cpp emits offsets as milliseconds (including values below 1000).
        if isinstance(offset, (int, float)):
            return round(float(offset))
        return _timestamp_ms(offset)
    timestamp = segment.get("timestamps", {}).get(edge) if isinstance(segment.get("timestamps"), dict) else None
    if timestamp is not None:
        return _timestamp_ms(timestamp)
    return _timestamp_ms(segment.get("start" if edge == "from" else "end"))


def parse_whisper_json(payload: dict[str, Any], *, allow_empty: bool = False) -> list[dict[str, Any]]:
    """Normalize whisper.cpp JSON timestamps to integer milliseconds."""
    raw_segments: Any = payload.get("transcription")
    if not isinstance(raw_segments, list):
        raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        result = payload.get("result")
        raw_segments = result.get("segments") if isinstance(result, dict) else None
    if not isinstance(raw_segments, list):
        raise RuntimeError("Whisper 已執行，但輸出 JSON 沒有可辨認的逐字稿段落。")

    normalized: list[dict[str, Any]] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        text = to_traditional_verbatim(str(item.get("text", "")).strip())
        if not text:
            continue
        start_ms = _pick_time(item, "from")
        end_ms = _pick_time(item, "to")
        if start_ms is None:
            start_ms = _timestamp_ms(item.get("start"))
        if end_ms is None:
            end_ms = _timestamp_ms(item.get("end"))
        if start_ms is None or end_ms is None:
            continue
        if end_ms < start_ms:
            end_ms = start_ms
        normalized.append({"start_ms": start_ms, "end_ms": end_ms, "text": text})
    normalized.sort(key=lambda segment: (segment["start_ms"], segment["end_ms"]))
    if not normalized and allow_empty and raw_segments == []:
        return []
    if not normalized:
        raise RuntimeError("Whisper 輸出中沒有有效的文字和時間戳，請檢查錄音格式或模型輸出。")
    return normalized


def _whisper_progress_fraction(line: str, duration_seconds: float | None) -> float | None:
    """Read progress reported by whisper.cpp, without estimating from elapsed time."""
    match = _WHISPER_PROGRESS_RE.search(line)
    if match:
        try:
            return max(0.0, min(1.0, float(match.group(1)) / 100.0))
        except ValueError:
            return None
    if duration_seconds is None or duration_seconds <= 0:
        return None
    match = _WHISPER_TIMESTAMP_RE.search(line)
    if not match:
        return None
    end_ms = _timestamp_ms(match.group(2))
    if end_ms is None:
        return None
    return max(0.0, min(1.0, end_ms / (duration_seconds * 1000)))


def _ffmpeg_progress_fraction(line: str, duration_seconds: float | None) -> float | None:
    """Read ffmpeg's progress protocol, using output time when duration is known."""
    if duration_seconds is None or duration_seconds <= 0:
        return None
    key, separator, value = line.partition("=")
    if not separator:
        return None
    key = key.strip()
    value = value.strip()
    if key == "out_time":
        elapsed_ms = _timestamp_ms(value)
    elif key == "out_time_us":
        try:
            elapsed_ms = round(int(value) / 1000)
        except ValueError:
            elapsed_ms = None
    elif key == "out_time_ms":
        try:
            # ffmpeg's progress protocol reports this legacy field in microseconds.
            elapsed_ms = round(int(value) / 1000)
        except ValueError:
            elapsed_ms = None
    else:
        return None
    if elapsed_ms is None:
        return None
    return max(0.0, min(1.0, elapsed_ms / (duration_seconds * 1000)))


def _transcribe_audio_single(
    audio_path: str | Path,
    work_dir: str | Path | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
    *,
    allow_empty: bool = False,
    input_is_normalized_wav: bool = False,
    decoding_args: list[str] | None = None,
    max_timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Convert one audio file to 16 kHz mono WAV and transcribe it locally."""
    source = Path(audio_path).resolve()
    if not source.is_file():
        raise RuntimeError("找不到課程錄音檔。請重新上傳錄音後再處理。")
    _notify_progress(progress_callback, 0.0, "正在準備音訊與 Whisper 模型")
    ffmpeg = _tool("ffmpeg")
    whisper = _tool(os.environ.get("WHISPER_CLI", "whisper-cli"))
    model_value = os.environ.get("WHISPER_MODEL", DEFAULT_MODEL)
    model = Path(model_value)
    if not model.is_absolute():
        model = (db.APP_ROOT / model).resolve()
    if not model.is_file():
        raise RuntimeError(
            f"找不到 Whisper 模型：{model}。模型完成下載後可直接重試，或用 WHISPER_MODEL 指定模型路徑。"
        )

    output_dir = Path(work_dir).resolve() if work_dir else source.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    wav_path = source if input_is_normalized_wav else output_dir / "transcription-16k-mono.wav"
    output_base = output_dir / "whisper-output"
    json_path = output_base.with_suffix(".json")
    try:
        _notify_progress(progress_callback, 0.01, "Whisper 模型已確認，正在準備音訊轉換")
        duration = _duration_seconds(source)
        _notify_progress(
            progress_callback,
            0.10 if input_is_normalized_wav else 0.02,
            "音訊轉換完成，正在準備轉錄" if input_is_normalized_wav else "正在轉換錄音格式",
        )

        def report_conversion(line_stream: str, line: str) -> None:
            del line_stream
            progress = _ffmpeg_progress_fraction(line, duration)
            if progress is not None:
                _notify_progress(
                    progress_callback,
                    min(0.099, 0.02 + 0.08 * progress),
                    "正在轉換錄音格式",
                )

        if not input_is_normalized_wav:
            conversion = _run_streaming(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
                 "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path)],
                timeout=None,
                line_callback=report_conversion,
            )
            if conversion.returncode != 0 or not wav_path.is_file():
                detail = (conversion.stderr or "").strip()[-1200:]
                raise RuntimeError("ffmpeg 無法轉換錄音。請確認檔案可播放且格式受支援。" + (f"\n{detail}" if detail else ""))
        _notify_progress(progress_callback, 0.10, "音訊轉換完成，正在準備轉錄")

        json_path.unlink(missing_ok=True)
        base_command = [whisper, "-m", str(model), "-f", str(wav_path), "-l", "zh", "-ojf", "-of", str(output_base), "-pp"]
        # Start at temperature zero; do not add translation, VAD, token
        # suppression or an invented filler-word prompt to force a transcript.
        base_command.extend(VERBATIM_DECODING_ARGS if decoding_args is None else decoding_args)
        vocabulary = os.environ.get("WHISPER_PROMPT", "").strip()
        if vocabulary:
            base_command.extend(["--prompt", vocabulary])
        threads = os.environ.get("WHISPER_THREADS", "8")
        if threads:
            try:
                thread_count = max(1, min(32, int(threads)))
                base_command[1:1] = ["-t", str(thread_count)]
            except ValueError:
                raise RuntimeError("WHISPER_THREADS 必須是 1 到 32 的整數。")
        try:
            timeout = max(60, min(24 * 60 * 60, int(os.environ.get("WHISPER_TIMEOUT_SECONDS", "14400"))))
        except ValueError:
            raise RuntimeError("WHISPER_TIMEOUT_SECONDS 必須是秒數。")
        if max_timeout_seconds is not None:
            timeout = min(timeout, max_timeout_seconds)
        force_cpu = os.environ.get("WHISPER_NO_GPU", "").strip().lower() in {"1", "true", "yes", "on"}
        command = [*base_command, *( ["-ng"] if force_cpu else [] )]
        started = time.monotonic()

        def run_whisper_attempt(attempt_command: list[str], attempt_timeout: float, label: str) -> subprocess.CompletedProcess[str]:
            last_observed_progress = -1.0
            _notify_progress(progress_callback, 0.15, f"正在以{label}啟動 Whisper 轉錄")

            def report_transcription(line_stream: str, line: str) -> None:
                nonlocal last_observed_progress
                del line_stream
                observed = _whisper_progress_fraction(line, duration)
                if observed is None or observed <= last_observed_progress:
                    return
                last_observed_progress = observed
                overall = min(0.95, 0.15 + 0.80 * observed)
                _notify_progress(
                    progress_callback,
                    overall,
                    f"Whisper 轉錄進度 {round(observed * 100)}%（{label}）",
                )

            return _run_streaming(
                attempt_command,
                timeout=attempt_timeout,
                line_callback=report_transcription,
            )

        try:
            recognition = run_whisper_attempt(command, timeout, "CPU" if force_cpu else "預設模式")
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"本機轉錄超過 {timeout // 60} 分鐘上限後停止；可縮短錄音或調整 WHISPER_TIMEOUT_SECONDS。") from exc
        except OSError as exc:
            raise RuntimeError(f"無法啟動 whisper-cli：{exc}") from exc
        if recognition.returncode != 0 and not force_cpu:
            # Metal can fail on memory allocation or a backend crash. Retry once in CPU mode.
            _notify_progress(progress_callback, 0.15, "Whisper 首次轉錄失敗，CPU fallback 正在重新開始")
            json_path.unlink(missing_ok=True)
            remaining = max(0, timeout - int(time.monotonic() - started))
            if remaining < 60:
                raise RuntimeError("Metal 轉錄失敗，且剩餘時間不足以啟動受時間限制的 CPU fallback。請設定 WHISPER_NO_GPU=1 後重試。")
            try:
                recognition = run_whisper_attempt([*base_command, "-ng"], remaining, "CPU")
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"whisper-cli CPU fallback 超過剩餘時間上限後停止。") from exc
            except OSError as exc:
                raise RuntimeError(f"無法啟動 whisper-cli CPU fallback：{exc}") from exc
        if recognition.returncode != 0 or not json_path.is_file():
            detail = (recognition.stderr or recognition.stdout or "").strip()[-1600:]
            raise RuntimeError("本機轉錄失敗。已在 Metal 失敗時嘗試一次 CPU；也可設定 WHISPER_NO_GPU=1。請確認模型完整並查看 whisper-cli 安裝。" + (f"\n{detail}" if detail else ""))
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"無法讀取 whisper-cli 輸出 JSON：{exc}") from exc
        segments = parse_whisper_json(payload, allow_empty=allow_empty)
        duration = _duration_seconds(source)
        if duration is None:
            duration = max((item["end_ms"] for item in segments), default=0) / 1000
        result = {"segments": segments, "duration_seconds": duration, "model": str(model),
                  "raw_outputs": [{"audio_start_ms": 0, "payload": payload}]}
        _notify_progress(progress_callback, 1.0, "轉錄完成")
        return result
    except OSError as exc:
        raise RuntimeError(f"無法執行音訊轉換或 whisper-cli：{exc}") from exc
    finally:
        # The converted WAV can be over 100 MB for one lecture. Source audio and
        # normalized transcript rows are enough to support a future retry.
        if not input_is_normalized_wav:
            wav_path.unlink(missing_ok=True)
        json_path.unlink(missing_ok=True)


def _transcribe_audio_impl(
    audio_path: str | Path,
    work_dir: str | Path | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
    chunk_minutes: int = 5,
    chunk_callback: Callable[[list[dict[str, Any]], int, int], None] | None = None,
) -> dict[str, Any]:
    """Transcribe locally, splitting longer recordings into independent chunks."""
    if type(chunk_minutes) is not int or chunk_minutes not in {5, 10}:
        raise ValueError("chunk_minutes 必須是 5 或 10。")

    source = Path(audio_path).resolve()
    if not source.is_file():
        raise RuntimeError("找不到課程錄音檔。請重新上傳錄音後再處理。")
    duration = _duration_seconds(source)
    if duration is None or duration <= 0:
        raise RuntimeError("無法判斷錄音長度，不能依指定的 5 或 10 分鐘分段轉錄。請確認 ffprobe 已安裝。")
    chunk_seconds = chunk_minutes * 60
    if duration <= chunk_seconds:
        result = _transcribe_audio_single(source, work_dir, progress_callback)
        if chunk_callback is not None:
            chunk_callback(result["segments"], 1, 1)
        return result

    output_dir = Path(work_dir).resolve() if work_dir else source.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = _tool("ffmpeg")
    overlap_seconds = 2.0
    chunk_count = math.ceil(duration / chunk_seconds)
    result_segments: list[dict[str, Any]] = []
    raw_outputs: list[dict[str, Any]] = []
    model = os.environ.get("WHISPER_MODEL", DEFAULT_MODEL)
    model_path = Path(model)
    if not model_path.is_absolute():
        model_path = (db.APP_ROOT / model_path).resolve()

    def mapped_progress(
        fraction: float,
        message: str,
        *,
        core_start: float,
        core_end: float,
        chunk_number: int,
    ) -> None:
        # Reserve the final 5% for validating and combining every chunk.
        fraction = max(0.0, min(1.0, fraction))
        source_fraction = (core_start + fraction * (core_end - core_start)) / duration
        overall = min(0.95, 0.15 + 0.80 * source_fraction)
        _notify_progress(progress_callback, overall, f"第 {chunk_number}/{chunk_count} 段：{message}")

    try:
        with tempfile.TemporaryDirectory(prefix="transcription-chunks-", dir=output_dir) as temporary_directory:
            chunk_dir = Path(temporary_directory)
            for chunk_index in range(chunk_count):
                core_start = chunk_index * chunk_seconds
                core_end = min(duration, core_start + chunk_seconds)
                audio_start = max(0.0, core_start - overlap_seconds)
                audio_end = min(duration, core_end + overlap_seconds)
                chunk_path = chunk_dir / f"chunk-{chunk_index:04d}.wav"
                mapped_progress(
                    0.0,
                    "正在準備音訊",
                    core_start=core_start,
                    core_end=core_end,
                    chunk_number=chunk_index + 1,
                )
                command = [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-y",
                    "-ss", f"{audio_start:.3f}", "-i", str(source), "-t", f"{audio_end - audio_start:.3f}",
                    "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(chunk_path),
                ]
                extraction = _run_streaming(command, timeout=None)
                if extraction.returncode != 0 or not chunk_path.is_file():
                    detail = (extraction.stderr or "").strip()[-1200:]
                    raise RuntimeError(
                        f"ffmpeg 無法準備第 {chunk_index + 1}/{chunk_count} 段音訊。"
                        + (f"\n{detail}" if detail else "")
                    )

                chunk_work_dir = chunk_dir / f"work-{chunk_index:04d}"
                chunk_start_ms = round(audio_start * 1000)
                core_start_ms = round(core_start * 1000)
                core_end_ms = round(core_end * 1000)

                def report_chunk_progress(fraction: float, message: str) -> None:
                    mapped_progress(
                        fraction, message,
                        core_start=core_start,
                        core_end=core_end,
                        chunk_number=chunk_index + 1,
                    )

                chunk_result = _transcribe_audio_single(
                    chunk_path,
                    chunk_work_dir,
                    report_chunk_progress,
                    allow_empty=True,
                    input_is_normalized_wav=True,
                )
                raw_outputs.extend({**raw, "audio_start_ms": chunk_start_ms + raw["audio_start_ms"],
                                    "core_start_ms": core_start_ms, "core_end_ms": core_end_ms}
                                   for raw in chunk_result.get("raw_outputs", []))
                is_last_chunk = chunk_index == chunk_count - 1
                completed_segments: list[dict[str, Any]] = []
                for segment in chunk_result["segments"]:
                    start_ms = max(0, chunk_start_ms + segment["start_ms"])
                    end_ms = min(round(duration * 1000), chunk_start_ms + segment["end_ms"])
                    if end_ms < start_ms:
                        end_ms = start_ms
                    midpoint_ms = (start_ms + end_ms) / 2
                    owned = core_start_ms <= midpoint_ms < core_end_ms
                    if is_last_chunk and midpoint_ms == core_end_ms:
                        owned = True
                    if not owned:
                        continue
                    completed_segments.append({
                        "start_ms": max(start_ms, core_start_ms),
                        "end_ms": min(end_ms, core_end_ms),
                        "text": segment["text"],
                    })
                result_segments.extend(completed_segments)
                if chunk_callback is not None:
                    chunk_callback(completed_segments, chunk_index + 1, chunk_count)

        result_segments.sort(key=lambda segment: (segment["start_ms"], segment["end_ms"]))
        if not result_segments:
            raise RuntimeError("Whisper 輸出中沒有有效的文字和時間戳，請檢查錄音格式或模型輸出。")
        _notify_progress(progress_callback, 1.0, "轉錄完成")
        return {"segments": result_segments, "duration_seconds": duration, "model": str(model_path),
                "raw_outputs": raw_outputs}
    except OSError as exc:
        raise RuntimeError(f"無法執行音訊轉換或 whisper-cli：{exc}") from exc


def transcribe_audio(
    audio_path: str | Path,
    work_dir: str | Path | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
    chunk_minutes: int = 5,
    chunk_callback: Callable[[list[dict[str, Any]], int, int], None] | None = None,
) -> dict[str, Any]:
    """Archive raw recognition separately from editable, script-converted text."""
    def report(fraction: float, message: str) -> None:
        _notify_progress(progress_callback, min(0.85, fraction * 0.85), message)

    result = _transcribe_audio_impl(audio_path, work_dir, report,
                                    chunk_minutes, chunk_callback)
    raw_outputs = result.pop("raw_outputs", [])
    if raw_outputs:
        flags = quality.review_flags(result["segments"], raw_outputs, result["duration_seconds"])
        try:
            review_limit = max(0, min(12, int(os.environ.get("WHISPER_REVIEW_LIMIT", "3"))))
        except ValueError:
            raise RuntimeError("WHISPER_REVIEW_LIMIT 必須是 0 到 12 的整數。")
        windows = quality.review_windows(flags, result["duration_seconds"], review_limit)
        reviews = []
        for index, window in enumerate(windows):
            _notify_progress(progress_callback, .85 + .13 * index / len(windows),
                             f"第二輪疑難短段複核 {index + 1}/{len(windows)}")
            review = dict(window)
            review["decoding_args"] = ["-bs", "8", "-tp", "0", "-nf"]
            review["first_text"] = "".join(s["text"] for s in result["segments"]
                                          if s["start_ms"] < window["end_ms"] and s["end_ms"] > window["start_ms"])
            try:
                with tempfile.TemporaryDirectory(prefix="whisper-review-") as temporary:
                    clip = Path(temporary) / "clip.wav"
                    extraction = _run_streaming([
                        _tool("ffmpeg"), "-v", "error", "-y", "-ss", str(window["start_ms"] / 1000),
                        "-i", str(Path(audio_path).resolve()), "-t", str((window["end_ms"] - window["start_ms"]) / 1000),
                        "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(clip)], timeout=60)
                    if extraction.returncode:
                        raise RuntimeError("複核片段擷取失敗")
                    second = _transcribe_audio_single(clip, temporary, allow_empty=True,
                        input_is_normalized_wav=True, decoding_args=review["decoding_args"],
                        max_timeout_seconds=180)
                    review.update(second_text="".join(s["text"] for s in second["segments"]),
                                  raw_outputs=second.get("raw_outputs", []), status="compared")
                    review["different"] = review["first_text"] != review["second_text"]
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                review.update(status="failed", error=str(exc))
            reviews.append(review)
        archive_dir = db.DATA_DIR / "transcription_raw"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive = archive_dir / f"{uuid.uuid4().hex}.json"
        record = {
            "source": str(Path(audio_path).resolve()), "source_offset_ms": 0,
            "duration_seconds": result["duration_seconds"],
            "model": result["model"], "language": "zh", "translate": False,
            "decoding_args": VERBATIM_DECODING_ARGS,
            "prompt": os.environ.get("WHISPER_PROMPT", "").strip(),
            "created_at_unix": time.time(), "raw_outputs": raw_outputs,
            "segments": result["segments"],
            "quality": {"flags": flags, "reviews": reviews,
                        "review_limit": review_limit, "requires_listening": True},
        }
        try:
            record["course_id"] = Path(audio_path).resolve().relative_to(db.DATA_DIR.resolve()).parts[0]
        except ValueError:
            record["course_id"] = None
        archive.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        result["raw_archive"] = str(archive)
    _notify_progress(progress_callback, 1.0, "轉錄完成")
    return result


def transcribe_audio_range(
    audio_path: str | Path,
    start_seconds: float,
    end_seconds: float,
    work_dir: str | Path | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
    chunk_minutes: int = 5,
    chunk_callback: Callable[[list[dict[str, Any]], int, int], None] | None = None,
) -> dict[str, Any]:
    """Extract and transcribe one bounded interval, returning original-timeline stamps."""
    if not math.isfinite(start_seconds) or not math.isfinite(end_seconds) or not 0 <= start_seconds < end_seconds:
        raise ValueError("重新轉錄的開始與結束時間無效。")
    source = Path(audio_path).resolve()
    if not source.is_file():
        raise RuntimeError("找不到課程錄音檔。請重新上傳錄音後再處理。")
    output_dir = Path(work_dir).resolve() if work_dir else source.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    start_ms, end_ms = round(start_seconds * 1000), round(end_seconds * 1000)
    if end_ms <= start_ms:
        raise ValueError("重新轉錄範圍必須至少長於一毫秒。")
    try:
        with tempfile.TemporaryDirectory(prefix="transcription-range-", dir=output_dir) as temporary_directory:
            temporary = Path(temporary_directory)
            clip = temporary / "selected-range.wav"
            command = [
                _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostats", "-y",
                "-ss", f"{start_seconds:.3f}", "-i", str(source),
                "-t", f"{end_seconds - start_seconds:.3f}",
                "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(clip),
            ]
            _notify_progress(progress_callback, 0.01, "正在擷取選取的錄音範圍")
            extraction = _run_streaming(command, timeout=None)
            if extraction.returncode != 0 or not clip.is_file():
                detail = (extraction.stderr or "").strip()[-1200:]
                raise RuntimeError("ffmpeg 無法擷取指定錄音範圍。" + (f"\n{detail}" if detail else ""))

            def report(fraction: float, message: str) -> None:
                _notify_progress(progress_callback, min(0.99, 0.05 + 0.94 * fraction), message)

            def to_absolute(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
                absolute_segments: list[dict[str, Any]] = []
                for segment in segments:
                    segment_start = max(start_ms, start_ms + int(segment["start_ms"]))
                    segment_end = min(end_ms, start_ms + int(segment["end_ms"]))
                    if segment_end <= segment_start:
                        continue
                    absolute_segments.append({
                        "start_ms": segment_start,
                        "end_ms": segment_end,
                        "text": segment["text"],
                    })
                return absolute_segments

            def report_chunk(segments: list[dict[str, Any]], chunk_number: int, total_chunks: int) -> None:
                if chunk_callback is not None:
                    chunk_callback(to_absolute(segments), chunk_number, total_chunks)

            result = transcribe_audio(
                clip, temporary / "work", report, chunk_minutes=chunk_minutes,
                **({"chunk_callback": report_chunk} if chunk_callback is not None else {}),
            )
            absolute_segments = to_absolute(result["segments"])
            if result.get("raw_archive"):
                archive = Path(result["raw_archive"])
                record = json.loads(archive.read_text(encoding="utf-8"))
                record.update(source=str(source), source_offset_ms=start_ms)
                archive.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            if not absolute_segments:
                raise RuntimeError("指定範圍沒有辨識出有效文字，原逐字稿已保留。")
            _notify_progress(progress_callback, 1.0, "選取範圍轉錄完成")
            return {"segments": absolute_segments, "duration_seconds": end_seconds - start_seconds,
                    "model": result["model"]}
    except OSError as exc:
        raise RuntimeError(f"無法擷取指定錄音範圍：{exc}") from exc
