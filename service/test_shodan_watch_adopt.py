"""録画引継ぎを、実プロセスやlsofを起動せず確かめる。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
# import時に利用者の設定を読ませない。保存先・一時領域もリポジトリ内。
with tempfile.TemporaryDirectory(prefix='.test-watch-import-', dir=str(REPO)) as directory:
    config = Path(directory) / 'config.json'
    config.write_text(json.dumps({'data_root': directory}), encoding='utf-8')
    with patch.dict(os.environ, {'MICA_CONFIG': str(config), 'MICA_APP_SUPPORT': directory,
                                 'HOME': directory}, clear=True), \
         patch('subprocess.run', side_effect=AssertionError('実プロセスは禁止')), \
         patch('urllib.request.urlopen', side_effect=AssertionError('外部通信は禁止')):
        import shodan_watch as w


class AdoptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='.test-watch-', dir=str(REPO))
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'MICA_CONFIG': str(Path(self.tmp.name) / 'config.json'),
                                          'MICA_APP_SUPPORT': self.tmp.name, 'HOME': self.tmp.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        w.mica_config.reload()
        self.addCleanup(w.mica_config.reload)
        self.old = w.ROOT
        w.ROOT = self.tmp.name
        self.addCleanup(setattr, w, 'ROOT', self.old)
        self.dir = self.recording('rec1', 12345)
        alive = patch.object(w, 'alive', return_value=True)
        self.alive = alive.start()
        self.addCleanup(alive.stop)
        run = patch.object(w.subprocess, 'run', side_effect=AssertionError('実プロセスは禁止'))
        self.run = run.start()
        self.addCleanup(run.stop)
        network = patch('urllib.request.urlopen', side_effect=AssertionError('外部通信は禁止'))
        network.start()
        self.addCleanup(network.stop)

    def recording(self, name, pid):
        folder = Path(self.tmp.name) / name
        folder.mkdir()
        (folder / 'video.mov').write_bytes(b'')
        (folder / 'video.mov.pid').write_text(str(pid), encoding='utf-8')
        (folder / 'meta.json').write_text(json.dumps({'recorded_at': '2026-11-05T00:51:00+09:00'}), encoding='utf-8')
        return str(folder)

    def response(self, output='', code=0, error=''):
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], code, output, error)

    def test_reused_pid_not_holding_file_is_not_adopted(self):
        self.response(code=1)
        self.assertEqual(w.adopt_recording(), (None, None, 0.0))
        arguments = self.run.call_args[0][0]
        self.assertEqual(arguments[-2:], ['--', str(Path(self.dir) / 'video.mov')])
        self.assertIn('-Ffa', arguments)

    def test_writer_is_adopted(self):
        self.response('p12345\nf7\naw\n')
        pid, folder, started = w.adopt_recording()
        self.assertEqual((pid, folder), (12345, self.dir))
        self.assertGreater(started, 0)

    def test_read_write_is_adopted(self):
        self.response('p12345\nf7\nau\n')
        self.assertEqual(w.adopt_recording()[:2], (12345, self.dir))

    def test_reader_is_not_adopted(self):
        self.response('p12345\nf7\nar\n')
        self.assertEqual(w.adopt_recording(), (None, None, 0.0))

    def test_unknown_or_failed_lsof_blocks_new_recording(self):
        for output, code, error in [('p98765\nf7\naw\n', 0, ''),
                                     ('p12345\nf7\n', 0, ''),
                                     ('p12345\nf7\na?\n', 0, ''),
                                     ('', 2, ''), ('', 1, 'status error')]:
            with self.subTest(output=output, code=code):
                self.response(output, code, error)
                with self.assertRaises(RuntimeError):
                    w.adopt_recording()

    def test_timeout_blocks_new_recording(self):
        self.run.side_effect = subprocess.TimeoutExpired('mock-lsof', 10)
        with self.assertRaises(RuntimeError):
            w.adopt_recording()

    def test_done_capture_is_not_probed(self):
        folder = Path(self.dir) / 'live-audio'
        folder.mkdir()
        (folder / 'capture.done').write_bytes(b'')
        self.assertEqual(w.adopt_recording(), (None, None, 0.0))
        self.run.assert_not_called()

    def test_dead_pid_is_not_probed(self):
        self.alive.return_value = False
        self.assertEqual(w.adopt_recording(), (None, None, 0.0))
        self.run.assert_not_called()

    def test_multiple_writers_block_new_recording(self):
        self.recording('rec2', 67890)
        def result(arguments, **_kwargs):
            pid = arguments[arguments.index('-p') + 1]
            return subprocess.CompletedProcess(arguments, 0, 'p' + pid + '\nf7\naw\n', '')
        self.run.side_effect = result
        with self.assertRaises(RuntimeError):
            w.adopt_recording()


if __name__ == '__main__':
    unittest.main()
