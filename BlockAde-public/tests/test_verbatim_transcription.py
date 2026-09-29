from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import transcribe


class VerbatimTranscriptionTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"WHISPER_REVIEW_LIMIT": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cpu_retry_retains_decoding_and_archives_unmodified_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, model = root / 'lecture.wav', root / 'large-v3.bin'
            source.write_bytes(b'audio')
            model.write_bytes(b'model')
            commands = []
            payload = {'transcription': [{'offsets': {'from': 0, 'to': 1000},
                                          'text': '嗯，软件、软件，不是，ECG。'}]}

            def run(command, **kwargs):
                if command[0] == 'ffmpeg':
                    Path(command[-1]).write_bytes(b'wav')
                    return subprocess.CompletedProcess(command, 0, '', '')
                commands.append(command)
                if '-ng' not in command:
                    return subprocess.CompletedProcess(command, 1, '', 'Metal failed')
                Path(command[command.index('-of') + 1]).with_suffix('.json').write_text(
                    json.dumps(payload), encoding='utf-8')
                return subprocess.CompletedProcess(command, 0, '', '')

            with (mock.patch.dict(os.environ, {'WHISPER_MODEL': str(model), 'WHISPER_REVIEW_LIMIT': '0',
                                               'WHISPER_PROMPT': 'ECG, electrocardiogram'}, clear=True),
                  mock.patch.object(db, 'DATA_DIR', root / 'data'),
                  mock.patch.object(transcribe, '_tool', side_effect=lambda name: name),
                  mock.patch.object(transcribe, '_duration_seconds', return_value=1.0),
                  mock.patch.object(transcribe, '_run_streaming', side_effect=run)):
                result = transcribe.transcribe_audio(source, root / 'work')
            self.assertEqual(len(commands), 2)
            for command in commands:
                self.assertEqual(command[command.index('-bs') + 1], '5')
                self.assertEqual(command[command.index('-tp') + 1], '0')
                self.assertEqual(command[command.index('-tpi') + 1], '0.2')
                self.assertEqual(command[command.index('-l') + 1], 'zh')
                self.assertEqual(command[command.index('--prompt') + 1], 'ECG, electrocardiogram')
                for flag in ('-tr', '--translate', '--vad', '-sns', '--suppress-regex'):
                    self.assertNotIn(flag, command)
            record = json.loads(Path(result['raw_archive']).read_text())
            self.assertEqual(record['raw_outputs'][0]['payload'], payload)
            self.assertEqual(record['source'], str(source.resolve()))
            self.assertEqual(result['segments'][0]['text'], '嗯，軟件、軟件，不是，ECG。')
            self.assertTrue(source.exists())

    def test_default_model_is_nonquantized_and_missing_model_does_not_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'lecture.wav'
            source.touch()
            with (mock.patch.dict(os.environ, {}, clear=True),
                  mock.patch.object(db, 'APP_ROOT', root),
                  mock.patch.object(transcribe, '_tool', side_effect=lambda name: name)):
                with self.assertRaisesRegex(RuntimeError, 'ggml-large-v3.bin'):
                    transcribe._transcribe_audio_single(source)

    def test_range_archive_identifies_original_recording_and_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'lecture.wav'
            source.touch()
            def extract(command, **kwargs):
                Path(command[-1]).touch()
                return subprocess.CompletedProcess(command, 0, '', '')
            result = {'segments': [{'start_ms': 1000, 'end_ms': 2000, 'text': '嗯。'}],
                      'model': 'large-v3.bin', 'duration_seconds': 10,
                      'raw_outputs': [{'audio_start_ms': 0, 'payload': {'text': '嗯。'}}]}
            with (mock.patch.object(db, 'DATA_DIR', root / 'data'),
                  mock.patch.object(transcribe, '_tool', return_value='ffmpeg'),
                  mock.patch.object(transcribe, '_run_streaming', side_effect=extract),
                  mock.patch.object(transcribe, '_transcribe_audio_impl', return_value=result)):
                actual = transcribe.transcribe_audio_range(source, 60, 70, root / 'work')
            self.assertEqual(actual['segments'][0]['start_ms'], 61000)
            archive = next((root / 'data' / 'transcription_raw').glob('*.json'))
            record = json.loads(archive.read_text())
            self.assertEqual(record['source'], str(source.resolve()))
            self.assertEqual(record['source_offset_ms'], 60000)
            self.assertEqual(record['segments'][0]['start_ms'], 1000)

    def test_raw_archive_survives_chunk_temporary_directory_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'lecture.wav'
            source.touch()
            payload = {'transcription': [{'text': '嗯，嗯。'}]}
            def extract(command, **kwargs):
                Path(command[-1]).touch()
                return subprocess.CompletedProcess(command, 0, '', '')
            def single(*args, **kwargs):
                return {'segments': [{'start_ms': 2000, 'end_ms': 2500, 'text': '嗯，嗯。'}],
                        'raw_outputs': [{'audio_start_ms': 0, 'payload': payload}]}
            with (mock.patch.object(db, 'DATA_DIR', root / 'data'),
                  mock.patch.object(transcribe, '_duration_seconds', return_value=301),
                  mock.patch.object(transcribe, '_tool', return_value='ffmpeg'),
                  mock.patch.object(transcribe, '_run_streaming', side_effect=extract),
                  mock.patch.object(transcribe, '_transcribe_audio_single', side_effect=single)):
                result = transcribe.transcribe_audio(source, root / 'work')
            record = json.loads(Path(result['raw_archive']).read_text())
            self.assertEqual([p['audio_start_ms'] for p in record['raw_outputs']], [0, 298000])
            self.assertEqual(list((root / 'work').iterdir()), [])
            self.assertEqual([s['text'] for s in result['segments']], ['嗯，嗯。', '嗯，嗯。'])
