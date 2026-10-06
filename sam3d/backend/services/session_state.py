"""대화형 세션이 살아 있다고 리퍼에게 알린다 — 기본은 꺼져 있다.

왜 필요한가. 리퍼(deploy/worker/idle_reaper.py)는 "큐가 비었고 워커 상태
파일이 전부 busy=false" 를 유휴로 보고 120초 뒤 인스턴스를 회수한다. 그
판단에 **백엔드(8001)의 대화형 트래픽은 전혀 들어가지 않는다.** 2단계는
큐를 쓰지 않고 백엔드를 직접 때리므로(프런트 utils/jobs.js 가 sam3dMesh·
omni3dEstimate·roomLayout 셋만 큐로 보낸다), 사용자가 가구를 고르고 있는
동안 큐는 계속 비어 있다. 그대로 두면 **세션 한가운데서 인스턴스가 내려간다.**

왜 기본값이 꺼짐인가. 리퍼의 EXPECT_WORKERS 에 'backend' 를 넣는 순간
반대쪽 고장 모드가 생긴다 — 상태 파일이 **없으면**(missing) 리퍼는 유휴
판정을 영원히 보류한다. 백엔드가 못 뜨는 날에는 빈 GPU 가 계속 돈다.
비용이 최우선인 구성에서 그건 조용히 새는 쪽 고장이다. 그래서 능력만
AMI 에 넣어 두고, 켜는 건 시작 템플릿에서 결정한다(재굽기 없이 바꾼다).

켜는 법 — 두 쪽을 **같이** 켜야 한다. 한쪽만 켜면 아무 효과가 없다.
  backend: EMR_BACKEND_STATE_FILE=/state/backend.state  (+ /state 마운트)
  reaper : EXPECT_WORKERS=emr-sam3d,emr-scene,backend

busy 의 뜻. "지금 요청을 처리중"이 아니라 "최근 EMR_BACKEND_BUSY_SEC 안에
요청이 있었다"이다. 클릭 사이의 생각하는 시간에 내려가면 안 되기 때문이다.
기본 600초는 residency.py 의 TTL 안전망과 같은 값으로 맞춰 뒀다 — 둘 다
"사용자가 2단계를 떠났다"를 같은 기준으로 본다.
"""
import json
import os
import time

STATE_FILE = os.environ.get('EMR_BACKEND_STATE_FILE', '')
BUSY_SEC = float(os.environ.get('EMR_BACKEND_BUSY_SEC', '600'))
# 리퍼의 REAPER_STALE_SEC 기본값은 90초다. 그보다 넉넉히 자주 써야 "응답없음"
# 으로 잘못 잡히지 않는다.
WRITE_SEC = float(os.environ.get('EMR_BACKEND_STATE_WRITE_SEC', '20'))


class SessionState:
    def __init__(self, path=STATE_FILE, busy_sec=BUSY_SEC):
        self.path = path
        self.busy_sec = busy_sec
        self.last_request = time.time()

    @property
    def enabled(self):
        return bool(self.path)

    def touch(self):
        self.last_request = time.time()

    def write(self):
        if not self.path:
            return
        busy = (time.time() - self.last_request) < self.busy_sec
        # 워커와 **같은 모양**으로 쓴다(worker.py 의 set_busy). 리퍼는 queue /
        # busy / updated_at 만 읽는다.
        body = json.dumps({"queue": "backend", "busy": busy,
                           "updated_at": int(time.time()), "pid": os.getpid()})
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # 같은 파일시스템 안의 rename 은 원자적이다. 바로 쓰면 리퍼가 반쯤
            # 쓰인 JSON 을 읽을 수 있고, 그걸 읽은 리퍼는 "못 읽으면 바쁜 것으로
            # 간주" 로 빠져 회수가 늦어진다.
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                f.write(body)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[session] 상태 파일 기록 실패(무시): {e}")
