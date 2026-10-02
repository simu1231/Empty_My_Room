"""
③ 재시도 분류 단위 테스트.

실행:  python3 deploy/tests/test_jobspec.py        (또는 -m unittest)

이 PC 에는 boto3 도 pytest 도 없다. 설치를 요구하면 "나중에 돌리지"가 되고
결국 안 돌린다. 그래서 stdlib unittest 만 쓰고, boto3 는 sys.modules 에
가짜를 꽂아 대신한다. worker.py 가 import 시점에 클라이언트를 만들기 때문에
이 가짜가 없으면 import 자체가 안 된다.

여기서 지키려는 것은 딱 하나다.
  **non-retryable 만 메시지를 지운다. 나머지는 절대 지우지 않는다.**
거꾸로 되면 CUDA OOM 같은 일시 오류로 사용자 작업이 영영 사라진다.
"""
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "worker"))


# ── boto3 가짜 ───────────────────────────────────────────────────────────
class FakeClient:
    """호출만 기록하는 녹음기. 반환값은 전부 빈 dict 면 충분하다."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*a, **kw):
            self.calls.append((name, a, kw))
            return {}
        return call

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


def _install_fake_boto3():
    s3, sqs, tbl = FakeClient(), FakeClient(), FakeClient()

    boto3 = types.ModuleType("boto3")
    boto3.client = lambda svc, **kw: {"s3": s3, "sqs": sqs}[svc]
    res = types.SimpleNamespace(Table=lambda name: tbl)
    boto3.resource = lambda svc, **kw: res

    botocore = types.ModuleType("botocore")
    cfg = types.ModuleType("botocore.config")
    cfg.Config = lambda **kw: None
    botocore.config = cfg

    sys.modules.update({"boto3": boto3, "botocore": botocore,
                        "botocore.config": cfg})
    return s3, sqs, tbl


FAKE_S3, FAKE_SQS, FAKE_TBL = _install_fake_boto3()

import jobspec            # noqa: E402
from jobspec import InvalidJob   # noqa: E402
import worker             # noqa: E402


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeHTTPError(Exception):
    """requests.HTTPError 처럼 .response.status_code 를 들고 있다."""

    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = FakeResponse(status_code)


# ── 1. 검증 규칙 ────────────────────────────────────────────────────────
class TestValidate(unittest.TestCase):

    def test_정상_작업은_통과한다(self):
        jobspec.validate("sam3d_mesh", {}, {"image": "k"})
        jobspec.validate("room_layout", {}, {"image": "k"})
        jobspec.validate("omni3d", {"bbox": "10,10,54,54", "category": "액자"},
                         {"image": "k"})

    def test_room_layout_은_camera_height_없이도_통과한다(self):
        # worker.py 가 기본값 1.6 을 쓴다. 필수로 적었다면 여기서 터졌을 것이다.
        jobspec.validate("room_layout", {}, {"image": "k"})

    def test_모르는_job_type(self):
        with self.assertRaises(InvalidJob):
            jobspec.validate("sam2_mask", {}, {"image": "k"})

    def test_omni3d_파라미터_누락(self):
        # 이번 부하 테스트에서 6건을 죽인 바로 그 입력이다.
        with self.assertRaises(InvalidJob) as cm:
            jobspec.validate("omni3d", {}, {"image": "k"})
        self.assertIn("bbox", str(cm.exception))
        self.assertIn("category", str(cm.exception))

    def test_빈_문자열은_누락으로_본다(self):
        for params in ({"bbox": "", "category": "액자"},
                       {"bbox": "1,1,2,2", "category": "   "}):
            with self.assertRaises(InvalidJob):
                jobspec.validate("omni3d", params, {"image": "k"})

    def test_bbox_형식(self):
        for bad in ("1,2,3", "1,2,3,4,5", "a,b,c,d", "10,10,10,54", "5,5,1,1"):
            with self.assertRaises(InvalidJob, msg=bad):
                jobspec.validate("omni3d", {"bbox": bad, "category": "액자"},
                                 {"image": "k"})

    def test_이미지_없음(self):
        with self.assertRaises(InvalidJob):
            jobspec.validate("sam3d_mesh", {}, {"mask": "k"})


# ── 2. 화이트리스트 분류 ────────────────────────────────────────────────
class TestClassify(unittest.TestCase):

    def test_InvalidJob_은_non_retryable(self):
        self.assertTrue(jobspec.is_non_retryable(InvalidJob("x")))

    def test_사이드카_4xx_는_non_retryable(self):
        self.assertTrue(jobspec.is_non_retryable(FakeHTTPError(400)))
        self.assertTrue(jobspec.is_non_retryable(FakeHTTPError(422)))

    def test_5xx_와_일시오류는_retryable(self):
        # 화이트리스트가 제대로 동작하는지 보는 핵심 테스트다. 여기 하나라도
        # True 가 나오면 일시 오류로 사용자 작업이 삭제된다.
        for exc in (FakeHTTPError(500), FakeHTTPError(503),
                    RuntimeError("CUDA out of memory"),
                    TimeoutError("sidecar timeout"),
                    ConnectionError("connection reset"),
                    OSError("disk full"),
                    ValueError("처음 보는 오류")):
            self.assertFalse(jobspec.is_non_retryable(exc), msg=repr(exc))


# ── 3. 워커가 실제로 지우는가 ───────────────────────────────────────────
class TestProcessDeletes(unittest.TestCase):

    def setUp(self):
        FAKE_SQS.calls.clear()
        FAKE_S3.calls.clear()
        self.statuses = []
        worker.set_status = lambda jid, st, **kw: self.statuses.append((st, kw))

    def _run(self, job, inference=None):
        import json as _json
        worker.run_inference = inference or (lambda *a, **kw: (b"{}", "application/json"))
        worker.process("http://q", {"Body": _json.dumps(job), "ReceiptHandle": "RH"})

    def _deleted(self):
        return bool(FAKE_SQS.named("delete_message"))

    def _last_status(self):
        return self.statuses[-1]

    def test_파라미터_누락이면_즉시_지운다(self):
        self._run({"job_id": "j1", "job_type": "omni3d",
                   "input_key": "in.png", "params": {}})
        st, kw = self._last_status()
        self.assertEqual(st, "failed")
        self.assertEqual(kw["failure_kind"], "non_retryable")
        self.assertTrue(self._deleted(), "non-retryable 인데 메시지를 안 지웠다")

    def test_검증은_S3_다운로드보다_앞이다(self):
        self._run({"job_id": "j2", "job_type": "omni3d",
                   "input_key": "in.png", "params": {}})
        self.assertEqual(FAKE_S3.named("download_file"), [],
                         "잘못된 입력인데 S3 에서 내려받았다")

    def test_일시오류는_지우지_않는다(self):
        def boom(*a, **kw):
            raise RuntimeError("CUDA out of memory")
        self._run({"job_id": "j3", "job_type": "sam3d_mesh",
                   "input_key": "in.png", "params": {}}, inference=boom)
        st, kw = self._last_status()
        self.assertEqual(st, "failed")
        self.assertEqual(kw["failure_kind"], "retryable")
        self.assertFalse(self._deleted(), "일시 오류인데 메시지를 지웠다 — 작업이 사라진다")

    def test_사이드카_4xx_는_지운다(self):
        def boom(*a, **kw):
            raise FakeHTTPError(400)
        self._run({"job_id": "j4", "job_type": "room_layout",
                   "input_key": "in.png", "params": {}}, inference=boom)
        self.assertEqual(self._last_status()[1]["failure_kind"], "non_retryable")
        self.assertTrue(self._deleted())

    def test_성공하면_지운다(self):
        self._run({"job_id": "j5", "job_type": "sam3d_mesh",
                   "input_key": "in.png", "params": {}})
        self.assertEqual(self._last_status()[0], "done")
        self.assertTrue(self._deleted())


if __name__ == "__main__":
    unittest.main(verbosity=2)
