"""
ulayout_boot 기동 래퍼 단위 테스트 — GPU 도 ~/uLayout 도 없는 곳에서 돈다.

확인하려는 것은 단 하나다: **웜업이 끝나야 READY 다.**
이게 틀리면 워커가 아직 안 덥혀진 사이드카에 첫 작업을 던지고,
그 요청이 SIDECAR_TIMEOUT(120초)에 걸려 죽는다.

    python3 -m unittest discover -s deploy/tests -v
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "worker"))

# numpy 는 있지만 없을 수도 있으니 더미 이미지 크기를 줄여 둔다.
os.environ.setdefault("ULAYOUT_WARM_SIZE", "64x48")

import ulayout_boot  # noqa: E402


class FakeRouter:
    def __init__(self):
        self.on_startup = ["원본 _load — 걷어내져야 한다"]
        self.routes = [types.SimpleNamespace(path="/health"),
                       types.SimpleNamespace(path="/infer")]


class FakeApp:
    """FastAPI 중 래퍼가 실제로 쓰는 부분만 흉내 낸다."""

    def __init__(self):
        self.router = FakeRouter()
        self.added = []
        self.startup = []

    def get(self, path):
        def deco(fn):
            self.added.append(path)
            self.router.routes.append(types.SimpleNamespace(path=path))
            self._health = fn
            return fn
        return deco

    def on_event(self, name):
        def deco(fn):
            self.startup.append(fn)
            return fn
        return deco


def fake_server(load_fails=False, warm_fails=False, post_fails=False):
    """~/uLayout/server.py 대역. 래퍼가 건드리는 이름만 갖춘다."""
    m = types.SimpleNamespace()
    m.app = FakeApp()
    m._net = None
    m.torch = types.SimpleNamespace(cuda=types.SimpleNamespace(empty_cache=lambda: None))
    m.calls = []

    def load_model():
        m.calls.append("load_model")
        if load_fails:
            raise RuntimeError("체크포인트를 못 읽었다")
        return "NET"

    def _run_boundary_inference(img_rgb, camera_height_m):
        m.calls.append("boundary")
        # 웜업은 전역 _net 을 통해 돈다. 그 전에 세워져 있어야 한다.
        assert m._net == "NET", "웜업이 _net 없이 호출됐다"
        if warm_fails:
            raise RuntimeError("CUDA error: no kernel image is available")
        h, w = img_rgb.shape[:2]
        return None, h, w, "coord", "phi", "cols", {"roll": 0.0}

    def estimate_room_dimensions(phi, cols, camera_height_m):
        m.calls.append("dims")
        if post_fails:
            raise ValueError("경계선을 못 찾았다")
        return {"width": 3.0}

    def boundary_to_pixel_rows(coord, phi, h, w):
        m.calls.append("rows")
        return [], [], []

    m.load_model = load_model
    m._run_boundary_inference = _run_boundary_inference
    m.estimate_room_dimensions = estimate_room_dimensions
    m.boundary_to_pixel_rows = boundary_to_pixel_rows
    return m


def boot(server):
    """install 한 뒤 startup 핸들러를 직접 돌린다(uvicorn 없이)."""
    state = ulayout_boot.install(server, import_sec=0.5)
    for fn in server.app.startup:
        fn()
    return state


class TestReady(unittest.TestCase):
    def test_정상이면_READY_가_된다(self):
        s = fake_server()
        state = boot(s)
        self.assertEqual(state["stage"], "ready")
        self.assertTrue(s.app._health()["ulayout_loaded"])
        self.assertEqual(s._net, "NET")

    def test_웜업_전에는_READY_가_아니다(self):
        s = fake_server()
        ulayout_boot.install(s, import_sec=0.5)
        # startup 을 아직 안 돌렸다 = 컨테이너는 떴지만 웜업 전
        self.assertEqual(s.app._health()["ulayout_loaded"], False)

    def test_웜업_중에는_net_이_차_있어도_READY_가_아니다(self):
        """가장 중요한 케이스. _net 으로 판단하면 여기서 틀린다."""
        s = fake_server()
        seen = {}

        orig = s._run_boundary_inference

        def spy(img, h):
            seen["net"] = s._net
            seen["loaded"] = s.app._health()["ulayout_loaded"]
            return orig(img, h)

        s._run_boundary_inference = spy
        boot(s)
        self.assertEqual(seen["net"], "NET", "웜업은 _net 을 쓴다")
        self.assertFalse(seen["loaded"], "그런데도 READY 로 보이면 안 된다")

    def test_웜업_실패면_READY_가_아니다(self):
        s = fake_server(warm_fails=True)
        state = boot(s)
        self.assertEqual(state["stage"], "failed")
        self.assertFalse(s.app._health()["ulayout_loaded"])
        self.assertIsNone(s._net, "실패하면 _net 을 되돌려야 한다")

    def test_모델_로드_실패도_READY_가_아니다(self):
        s = fake_server(load_fails=True)
        state = boot(s)
        self.assertEqual(state["stage"], "failed")
        self.assertFalse(s.app._health()["ulayout_loaded"])

    def test_실패해도_프로세스를_죽이지_않는다(self):
        """죽으면 compose 가 재시작시키며 같은 실패를 무한 반복한다."""
        s = fake_server(warm_fails=True)
        state = boot(s)  # 예외가 새어 나오면 여기서 터진다
        self.assertIn("CUDA error", state["error"])

    def test_후처리만_실패하면_READY_로_간다(self):
        """합성 사진이라 치수 추정이 실패할 수 있다 — GPU 고장이 아니다."""
        s = fake_server(post_fails=True)
        state = boot(s)
        self.assertEqual(state["stage"], "ready")
        self.assertFalse(state["warm_post_ok"])


class TestInstall(unittest.TestCase):
    def test_원본_startup_핸들러를_걷어낸다(self):
        s = fake_server()
        ulayout_boot.install(s)
        self.assertNotIn("원본 _load — 걷어내져야 한다", s.app.router.on_startup)

    def test_health_라우트를_하나만_남긴다(self):
        s = fake_server()
        ulayout_boot.install(s)
        paths = [r.path for r in s.app.router.routes]
        self.assertEqual(paths.count("/health"), 1,
                         "원본 /health 가 남아 있으면 먼저 등록된 쪽이 이긴다")
        self.assertIn("/infer", paths, "다른 라우트는 건드리면 안 된다")

    def test_전체_infer_경로를_탄다(self):
        s = fake_server()
        boot(s)
        self.assertEqual(s.calls, ["load_model", "boundary", "dims", "rows"])


class TestDummyImage(unittest.TestCase):
    def test_요청한_해상도로_만든다(self):
        img = ulayout_boot._dummy_room_rgb(1280, 960)
        self.assertEqual(img.shape, (960, 1280, 3))

    def test_난수가_아니라_방처럼_생겼다(self):
        """천장/벽/바닥이 서로 다른 색이어야 경계 추정이 헛돌지 않는다."""
        img = ulayout_boot._dummy_room_rgb(100, 100)
        ceiling, wall, floor = img[5, 50], img[50, 50], img[90, 50]
        self.assertFalse((ceiling == wall).all())
        self.assertFalse((wall == floor).all())


if __name__ == "__main__":
    unittest.main(verbosity=2)
