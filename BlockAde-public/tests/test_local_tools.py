import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import audio_merge
import local_tools


class LocalAudioToolTests(unittest.TestCase):
    def test_probe_and_merge_work_without_tools_on_path(self):
        root = Path(local_tools.__file__).resolve().parent
        if not all((root / '.tools/bin' / name).is_file() for name in ('ffmpeg', 'ffprobe')):
            self.skipTest('Project-local audio tools required')
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}):
            self.assertTrue(local_tools.find_executable('ffprobe').startswith(str(root)))
            self.assertIsNone(local_tools.find_executable('/missing/ffprobe'))
            parts = []
            for index in range(2):
                p = Path(directory) / f'{index}.m4a'
                subprocess.run([local_tools.find_executable('ffmpeg'), '-v', 'error',
                                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.5',
                                '-c:a', 'aac', str(p)], check=True)
                self.assertAlmostEqual(audio_merge.probe_audio(p), 0.5, delta=0.1)
                parts.append(p)
            duration = audio_merge.merge_audio(parts, Path(directory) / 'merged.wav')
            self.assertAlmostEqual(duration, 1.0, delta=0.2)
