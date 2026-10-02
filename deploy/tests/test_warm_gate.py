"""
① 워밍 게이트 단위 테스트 — 파일 하나가 생길 때까지 작업 수신을 미루는 부분.

여기서 지키려는 것은 두 가지다.
  1. 플래그가 생기기 전에는 **안 받는다**(그게 게이트의 전부다).
  2. 플래그가 끝내 안 생겨도 **영원히 막히지 않는다**. 상한을 넘기면
     경고만 남기고 그냥 시작한다 — 안 그러면 호스트 쪽 버그 하나가
     인스턴스를 통째로 벙어리로 만들고, 리퍼가 유휴로 보고 회수한다.
     (요금은 나가고 일은 안 한 상태로 끝난다. 제일 비싼 실패다.)

실제 플래그를 만드는 쪽은 userdata.sh 의 systemd ExecStopPost 다. 여기서는
그 파일이 '생긴다/안 생긴다'만 흉내 내면 된다 — sleep 기반으로 충분하다.

실행:  python3 deploy/tests/test_warm_gate.py
"""
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "worker"))

# boto3 가짜를 먼저 꽂아야 worker 가 import 된다(test_jobspec 머리말 참고).
# 같은 가짜를 두 벌 두면 한쪽만 고치는 날이 온다 — 재사용한다.
from test_jobspec import _install_fake_boto3  # noqa: E402

_install_fake_boto3()
import worker  # noqa: E402

POLL = 0.01  # 테스트는 빨라야 돌린다. 실제 기본값은 1초다.


class GateCase(unittest.TestCase):
    """게이트 설정을 건드리므로 매번 원래대로 돌려놓는다."""

    def setUp(self):
        self._saved = (worker.WARM_GATE_FILE, worker.WARM_GATE_MAX_SEC)
        self.tmp = tempfile.mkdtemp(prefix="emr-gate-")
        self.flag = os.path.join(self.tmp, "warm-bg.done")
        worker.WARM_GATE_FILE = self.flag
        worker.WARM_GATE_MAX_SEC = 5
        worker._shutdown.clear()

    def tearDown(self):
        worker.WARM_GATE_FILE, worker.WARM_GATE_MAX_SEC = self._saved
        worker._shutdown.clear()
        for p in (self.flag, self.tmp):
            try:
                os.remove(p) if os.path.isfile(p) else os.rmdir(p)
            except OSError:
                pass

    def run_gate(self, **kw):
        """경고/진행 로그가 테스트 출력에 섞이지 않게 삼키고 같이 돌려준다."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            waited = worker.wait_for_warm_gate(poll=POLL, **kw)
        return waited, buf.getvalue()


class TestGateOff(GateCase):
    def test_설정이_비면_그냥_통과한다(self):
        """로컬·LocalStack 에는 호스트 워밍 자체가 없다. 여기서 막히면 안 된다."""
        worker.WARM_GATE_FILE = ""
        waited, _ = self.run_gate()
        self.assertEqual(waited, 0.0)

    def test_플래그_디렉터리가_없으면_통과한다(self):
        """옛 유저데이터로 뜬 인스턴스에는 /run/emr 이 없다. 그때도 돌아야 한다."""
        worker.WARM_GATE_FILE = "/run/없는디렉터리/warm-bg.done"
        waited, out = self.run_gate()
        self.assertEqual(waited, 0.0)
        self.assertIn("게이트 꺼짐", out)


class TestGateWaits(GateCase):
    def test_이미_끝나_있으면_안_기다린다(self):
        open(self.flag, "w").close()
        beats = []
        waited, _ = self.run_gate(heartbeat=lambda: beats.append(1))
        self.assertLess(waited, 0.5)
        self.assertEqual(beats, [], "기다리지 않았으면 하트비트도 없어야 한다")

    def test_나중에_생기면_그때_연다(self):
        delay = 0.3

        def late():
            time.sleep(delay)
            open(self.flag, "w").close()

        t = threading.Thread(target=late)
        t.start()
        waited, out = self.run_gate()
        t.join()
        self.assertGreaterEqual(waited, delay,
                                "플래그가 생기기 전에 열렸다 — 게이트가 무의미하다")
        self.assertLess(waited, worker.WARM_GATE_MAX_SEC)
        self.assertIn("워밍 끝", out)

    def test_기다리는_동안_하트비트를_친다(self):
        """리퍼는 이 신호가 끊기면 '죽었다'고 본다. 대기 중에도 쳐야 한다."""
        beats = []

        def late():
            time.sleep(0.2)
            open(self.flag, "w").close()

        t = threading.Thread(target=late)
        t.start()
        self.run_gate(heartbeat=lambda: beats.append(1))
        t.join()
        self.assertGreater(len(beats), 1)


class TestGateNeverOpens(GateCase):
    def test_상한을_넘기면_경고하고_시작한다(self):
        """제일 중요한 케이스. 여기서 막히면 인스턴스가 통째로 논다."""
        worker.WARM_GATE_MAX_SEC = 0.3
        t0 = time.monotonic()
        waited, out = self.run_gate()
        self.assertGreaterEqual(waited, 0.3)
        self.assertLess(time.monotonic() - t0, 3.0, "상한을 안 지켰다")
        self.assertIn("초과", out)
        self.assertIn("플래그 버그", out, "왜 열렸는지 로그만 보고 알 수 있어야 한다")

    def test_종료_신호가_오면_즉시_빠진다(self):
        """SIGTERM 을 받고도 게이트에 붙들려 있으면 stop_grace_period 를 넘긴다."""
        worker.WARM_GATE_MAX_SEC = 60

        def stop():
            time.sleep(0.2)
            worker._shutdown.set()

        t = threading.Thread(target=stop)
        t.start()
        t0 = time.monotonic()
        self.run_gate()
        t.join()
        self.assertLess(time.monotonic() - t0, 3.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
