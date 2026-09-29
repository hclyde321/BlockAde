"""Evidence-based review flags and subtitle exports; never rewrite speech."""
from __future__ import annotations
import math
import re


def review_flags(segments, raw_outputs, duration_seconds):
    duration = round(duration_seconds * 1000)
    flags = []
    def add(start, end, reason, detail):
        flags.append({'start_ms': max(0, int(start)), 'end_ms': min(duration, int(end)),
                      'reason': reason, 'detail': detail})
    for chunk in raw_outputs:
        offset = chunk['audio_start_ms']
        for row in chunk['payload'].get('transcription', []):
            scores = [t['p'] for t in row.get('tokens', [])
                      if isinstance(t.get('p'), (int, float)) and math.isfinite(t['p'])
                      and 0 <= t['p'] <= 1 and t.get('text', '').strip()
                      and not t.get('text', '').startswith(('[_', '<|'))]
            times = row.get('offsets', {})
            if scores and (sum(scores) / len(scores) < .65 or min(scores) < .25):
                start, end = times.get('from'), times.get('to')
                if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                    add(offset + start, offset + end, 'low_confidence',
                        '模型 token 分數偏低；只是複核線索，不是正確率。')
        boundary = chunk.get('core_start_ms', offset)
        if boundary > 0:
            add(boundary - 2000, boundary + 2000, 'boundary', '分段交界，請核對兩側是否缺字或重複。')
    previous = None
    coverage_end = 0
    for row in segments:
        start, end = row['start_ms'], row['end_ms']
        if end <= start:
            add(start, start + 1000, 'timestamp', '段落沒有有效時長。')
        if previous and start < previous['start_ms']:
            add(start, end, 'timestamp', '時間戳倒退。')
        if start - coverage_end >= 10000:
            add(coverage_end, start, 'gap', '這段沒有辨識文字，可能是靜音或漏字；需聽錄音確認。')
        if previous and re.sub(r'\W', '', row['text']) == re.sub(r'\W', '', previous['text']) and row['text'].strip():
            add(previous['start_ms'], end, 'repeat', '相鄰文字相同，可能是原話重複或辨識重複；不自動刪除。')
        coverage_end = max(coverage_end, end)
        previous = row
    if duration - coverage_end >= 10000:
        add(coverage_end, duration, 'gap', '尾段沒有辨識文字，請確認是否只有靜音。')
    return flags


def review_windows(flags, duration_seconds, limit=3):
    duration = round(duration_seconds * 1000)
    ranked = sorted(flags, key=lambda f: ({'low_confidence': 0, 'repeat': 1, 'boundary': 2}.get(f['reason'], 3), f['start_ms']))
    if not ranked and duration:
        ranked = [{'start_ms': duration // 2, 'end_ms': duration // 2, 'reason': 'spot_check'}]
    windows = []
    for flag in ranked:
        center = (flag['start_ms'] + flag['end_ms']) // 2
        start = max(0, min(center - 15000, duration - 30000))
        end = min(duration, start + 30000)
        if any(start < w['end_ms'] and end > w['start_ms'] for w in windows):
            continue
        if len(windows) >= limit:
            break
        windows.append({'start_ms': start, 'end_ms': end, 'reason': flag['reason']})
    return windows


def timestamp(ms, separator='.'):
    ms = max(0, round(ms))
    hours, rest = divmod(ms, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, milliseconds = divmod(rest, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02}{separator}{milliseconds:03}'


def subtitle(segments, kind, offset=0):
    lines = ['WEBVTT', ''] if kind == 'vtt' else []
    for i, row in enumerate(segments, 1):
        if kind == 'txt':
            lines.append(row['text'])
            continue
        if kind == 'srt':
            lines.append(str(i))
        sep = ',' if kind == 'srt' else '.'
        lines.extend([f"{timestamp(row['start_ms'] + offset, sep)} --> {timestamp(row['end_ms'] + offset, sep)}",
                      row['text'].replace('-->', '→'), ''])
    return '\n'.join(lines) + '\n'


def raw_segments(record):
    rows = []
    for chunk in record['raw_outputs']:
        shift = chunk['audio_start_ms']
        for row in chunk['payload'].get('transcription', []):
            times = row.get('offsets', {})
            if not isinstance(times.get('from'), (int, float)) or not isinstance(times.get('to'), (int, float)):
                continue
            start, end = round(times['from'] + shift), round(times['to'] + shift)
            midpoint = (start + end) / 2
            if not chunk.get('core_start_ms', 0) <= midpoint < chunk.get('core_end_ms', float('inf')):
                continue
            rows.append({'start_ms': start, 'end_ms': end, 'text': row.get('text', '').strip()})
    return sorted(rows, key=lambda s: (s['start_ms'], s['end_ms']))


def latest_record(directory, course_id):
    import json
    latest = None
    for path in directory.glob('*.json'):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if record.get('course_id') == course_id and (latest is None or record.get('created_at_unix', 0) > latest.get('created_at_unix', 0)):
            latest = record
    return latest
