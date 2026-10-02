"""SAM2/LaMa 상주 관리 — 쓸 때 올리고, 안 쓰면 내린다.

왜 필요한가. 이 백엔드는 SAM2(857MB) + LaMa(391MB)를 시작할 때 올려서 끝까지
붙들고 있었다. 그런데 둘을 쓰는 건 2단계(가구 선택 → 빈방 만들기)뿐이고,
3단계(uLayout)와 4단계(Omni3D + SAM3D)는 쓰지 않는다. 같은 GPU에 사이드카
둘과 워커가 함께 올라가면 여유가 1.7GB까지 떨어지고(RTX 4090 24.5GB 실측
22,398/24,564 사용), 그 상태에서 프로세스 간 GPU 작업이 겹치면 SAM3D decode가
0.2초 → 67초로 튄다. main.py 가 SD를 온디맨드로 돌린 것과 같은 이유다.

왜 타이머만으로 하지 않는가. 2단계는 클릭마다 /api/segment/mask 를 때리는
대화형 루프다(SegmentStep.jsx 의 300ms debounce). 짧은 TTL(90초)을 걸면
사용자가 가구를 고르다 잠깐 멈춘 사이에 내려가고, 다음 클릭이 재로드를
기다린다 — uvicorn reload 실측으로 가중치 로드 구간만 5.8초였고, 페이지
캐시가 식은 AWS EBS에서는 12~20초로 본다. 반대로 TTL을 길게 잡으면 4단계
SAM3D가 시작될 때까지 안 내려가서 목적을 잃는다.

그래서 둘을 나눴다.
  * 명시적 해제: 2 → 3 단계 전이에서 프런트가 /api/segment/release 를 부른다.
    "더 이상 안 쓴다"가 코드로 확정되는 유일한 지점이라(handleExtract 가
    inpaint → extract 를 끝내고 setStep('roommaking') 한다) 추측이 필요 없다.
  * TTL: 사용자가 2단계에서 창을 닫고 가버린 경우만 회수하는 안전망.
    기본 600초로 길게 둔다 — 대화형 클릭을 방해하지 않을 만큼.

해제량은 가중치 약 1.25GB다. CUDA 컨텍스트(약 430MB)는 프로세스가 살아 있는
동안 남으므로, 프로세스를 죽였을 때의 1.64GB 와 혼동하지 말 것.
"""
import gc
import os
import threading
import time
from contextlib import contextmanager

# 안전망 TTL. 0 이하면 타이머를 끈다(명시적 해제만 쓴다).
TTL_SEC = float(os.environ.get('EMR_SEG_TTL_SEC', '600'))
# 스위퍼가 깨어나는 간격. TTL 정밀도가 이 값만큼 거칠어진다.
SWEEP_SEC = float(os.environ.get('EMR_SEG_SWEEP_SEC', '30'))

_SLOTS = ('sam2', 'lama')


class SegmentResidency:
    def __init__(self, app, ttl_sec=TTL_SEC):
        self.app = app
        self.ttl_sec = ttl_sec
        # RLock 인 이유: use() 가 ensure() 를 부르고 둘 다 같은 락을 잡는다.
        self._lock = threading.RLock()
        self._inflight = 0
        self._last_used = time.monotonic()

    # ── 로드 ────────────────────────────────────────────────────────────
    def _build(self, name):
        if name == 'sam2':
            from services.sam2_service import SAM2Service
            return SAM2Service()
        from services.lama_service import LamaService
        return LamaService()

    def ensure(self, name):
        """없으면 올려서 돌려준다. 락을 쥔 채로 올리는 게 의도다 — 동시에
        들어온 두 요청이 같은 모델을 두 번 올리면 VRAM 이 두 배로 든다.

        use() 와 달리 진행중 표시를 세지 않는다. 그래도 요청이 깨지지는
        않는다 — 돌려준 객체를 호출자의 지역 변수가 강한 참조로 들고 있어서,
        그 사이 release() 가 app.state 를 비워도 객체 자체는 살아 있다.
        해제가 실제로 메모리를 돌려주는 시점만 그 요청이 끝난 뒤로 밀린다.
        그래서 긴 함수 전체가 모델을 쓰는 곳(extract, room.generate3d)은
        들여쓰기를 바꾸지 않고 이걸 쓰고, 짧은 구간은 use() 로 감싼다.
        """
        with self._lock:
            obj = getattr(self.app.state, name, None)
            if obj is not None:
                return obj
            t0 = time.time()
            obj = self._build(name)
            setattr(self.app.state, name, obj)
            print(f"[residency] {name} 재로드 완료 — {time.time()-t0:.2f}초")
            return obj

    @contextmanager
    def use(self, name):
        """모델을 쓰는 구간. 이 안에서는 해제가 일어나지 않는다."""
        obj = self.ensure(name)
        with self._lock:
            self._inflight += 1
        try:
            yield obj
        finally:
            with self._lock:
                self._inflight -= 1
                self._last_used = time.monotonic()

    # ── 해제 ────────────────────────────────────────────────────────────
    def release(self, reason):
        """SAM2/LaMa 를 내린다. 진행중인 요청이 있으면 건드리지 않는다 —
        추론 도중에 참조를 끊으면 그 요청이 죽는다."""
        with self._lock:
            if self._inflight > 0:
                print(f"[residency] 해제 보류({reason}) — 진행중 요청 {self._inflight}건")
                return {"released": [], "skipped": "in_flight", "inflight": self._inflight}
            freed = [n for n in _SLOTS if getattr(self.app.state, n, None) is not None]
            for n in freed:
                setattr(self.app.state, n, None)
            self._last_used = time.monotonic()

        if not freed:
            return {"released": [], "skipped": "already_free"}

        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
        print(f"[residency] 해제({reason}) — {', '.join(freed)} / GPU 메모리 반환")
        return {"released": freed}

    # ── 안전망 ──────────────────────────────────────────────────────────
    def idle_sec(self):
        with self._lock:
            return time.monotonic() - self._last_used

    def sweep(self):
        """TTL 을 넘겼으면 해제한다. 스위퍼 루프가 주기적으로 부른다."""
        if self.ttl_sec <= 0:
            return None
        with self._lock:
            idle = time.monotonic() - self._last_used
            if idle < self.ttl_sec or self._inflight > 0:
                return None
            if not any(getattr(self.app.state, n, None) for n in _SLOTS):
                return None
        return self.release(f"TTL {self.ttl_sec:.0f}초 초과, 유휴 {idle:.0f}초")

    def status(self):
        with self._lock:
            return {
                "sam2": getattr(self.app.state, 'sam2', None) is not None,
                "lama": getattr(self.app.state, 'lama', None) is not None,
                "inflight": self._inflight,
                "idle_sec": round(time.monotonic() - self._last_used, 1),
                "ttl_sec": self.ttl_sec,
            }
