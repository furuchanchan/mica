"""adopt_recording の試験。
録画の pid の印が残り、その pid が別のプロセスに使い回されているとき（録画ファイルを開いていない）は、
例外を出さず「録画なし」と判定すること。lsof の -t をファイル名の後ろに置くと、-t がファイル名として読まれ
『status error on -t』で毎回落ち、見張りが launchd の再起動で落ち続けていた。"""
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shodan_watch as w


class AdoptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = w.ROOT
        w.ROOT = self.tmp.name
        d = os.path.join(self.tmp.name, "rec1")
        os.mkdir(d)
        open(os.path.join(d, "video.mov"), "w").close()
        json.dump({"recorded_at": "2026-11-05T00:51:00+09:00"}, open(os.path.join(d, "meta.json"), "w"))
        self.dir = d

    def tearDown(self):
        w.ROOT = self.old
        self.tmp.cleanup()

    def test_reused_pid_not_holding_file_is_not_adopted(self):
        open(os.path.join(self.dir, "video.mov.pid"), "w").write(str(os.getpid()))
        self.assertEqual(w.adopt_recording(), (None, None, 0.0))

    def test_pid_holding_file_is_adopted(self):
        fh = open(os.path.join(self.dir, "video.mov"), "a")  # 自分がファイルを開いている＝録画中
        try:
            open(os.path.join(self.dir, "video.mov.pid"), "w").write(str(os.getpid()))
            pid, folder, started = w.adopt_recording()
            self.assertEqual((pid, folder), (os.getpid(), self.dir))
        finally:
            fh.close()


if __name__ == "__main__":
    unittest.main()
