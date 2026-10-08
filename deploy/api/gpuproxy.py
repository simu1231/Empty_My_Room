"""
GPU 워커를 찾아서 요청을 그대로 넘긴다.

■ 왜 프록시가 필요한가
지금 프런트엔드는 브라우저에서 GPU 워커의 :8001 을 직접 부른다. 그러려면
워커의 공인 IP 가 필요하고, 워커는 스케일투제로라 뜰 때마다 IP 가 바뀐다.
그 IP 를 브라우저에 알려주려면 결국 누군가 중계해야 하는데, 그 "누군가"는
상시 가동이어야 한다 — 즉 API 서버다.

덤으로 보안 경계가 하나 생긴다. 워커의 8001 을 0.0.0.0/0 에 열 필요가 없고,
emr-api-sg 에서만 들어오게 막을 수 있다. 8002/8003(uLayout/Omni3D)은 아예
안 연다 — 백엔드가 컨테이너 네트워크 안에서 부르는 포트지 바깥에서 부를
포트가 아니다.

■ IP 를 어떻게 찾는가
ec2:DescribeInstances 로 ASG 이름 태그를 보고 고른다. DynamoDB 자가등록
방식(워커가 부팅 때 자기 IP 를 써 넣는 것)도 있지만 쓰지 않기로 했다 —
테이블 하나와 쓰기 경로 하나가 더 늘고, 워커가 비정상 종료하면 유령 레코드가
남는다. EC2 가 이미 알고 있는 사실을 두 번 적을 이유가 없다.

주의: ec2:DescribeInstances 는 **자원 수준 권한도 태그 조건 키도 지원하지
않는다.** IAM 에서 Resource:"*" 가 강제되고, 걸러내기는 전부 여기(클라이언트
쪽)에서 한다. 그래서 필터를 느슨하게 쓰면 **남의 인스턴스를 프록시 대상으로
고르는** 사고가 난다 — ASG 이름과 Project 태그를 **둘 다** 본다.
"""
import base64
import os
import threading
import time

import boto3
import httpx
from botocore.config import Config

REGION   = os.getenv("AWS_REGION", "ap-northeast-2")
ASG_NAME = os.getenv("ASG_SPOT", "")
PROJECT  = os.getenv("PROJECT", "emr")
GPU_PORT = int(os.getenv("GPU_PORT", "8001"))

# 상류 대기 한도. CloudFront 의 오리진 응답 타임아웃이 60초다(2026-10-08 에
# 배포 EV58ZFPE0JICM 의 api-origin 에서 실측). 그걸 넘기면 CloudFront 가 연결을
# 끊고, 그 상태는 프런트에서 502/504 로 보여 원인을 알 수 없다. 우리가 먼저
# 포기하고 503 + Retry-After 로 바꿔 주면, 같은 상황이 "아직 준비 중"이라는
# **해석 가능한** 응답이 된다.
#
# 50 인 이유. 25 로는 여유가 2.2초뿐이라 실제로 503 이 났고, 더 나쁘게는 첫
# 요청이 25초를 넘기는 동안 뒤따라온 재시도까지 줄을 서다 같이 25초를 태워
# 연쇄로 터졌다. 그래서 45 로 올렸는데, 2026-10-08 실측에서 첫 인페인팅이
# 43.55초가 나와 여유가 1.45초밖에 안 남았다. 데워진 뒤는 24.92초였다.
#
# 한 가지 더 걸리는 게 있다. 같은 측정에서 워커가 보고한 처리시간과 클라이언트
# 실측의 간극이 데워진 요청은 1.75초인데 첫 요청만 5.77초였다(37.78 ↔ 43.55).
# 전송량은 같으니 전송 탓이 아니고, 원인은 아직 모른다. 이 간극이 상황에 따라
# 더 벌어지는 종류라면 1.45초로는 못 버틴다.
#
# 50 이면 첫 요청 여유가 6.5초, 데워진 요청은 25초다. 그러면서도 CloudFront
# 60 보다 10초 먼저 포기하므로 위 설계 의도는 그대로다.
UPSTREAM_TIMEOUT = float(os.getenv("GPU_UPSTREAM_TIMEOUT", "50"))

# 조회 결과 캐시. 폴링이 2초 간격이라 캐시가 없으면 DescribeInstances 를
# 초당 몇 번씩 부르게 된다(요청 제한에 걸린다). 성공은 조금 길게, 실패는
# 짧게 — 워커가 막 떴을 때 10초나 더 0대로 보이면 안 되기 때문이다.
HIT_TTL  = float(os.getenv("GPU_CACHE_TTL", "10"))
MISS_TTL = float(os.getenv("GPU_CACHE_MISS_TTL", "2"))

_lock = threading.Lock()
_cache: tuple[float, str | None] = (0.0, None)   # (만료시각, private IP)
_ec2 = None
_client: httpx.Client | None = None


def _ec2_client():
    global _ec2
    if _ec2 is None:
        _ec2 = boto3.client("ec2", config=Config(
            region_name=REGION, retries={"max_attempts": 2, "mode": "standard"}))
    return _ec2


def _http() -> httpx.Client:
    """연결 재사용. 매 요청마다 새 클라이언트를 만들면 TCP 핸드셰이크가 쌓인다."""
    global _client
    if _client is None:
        _client = httpx.Client(timeout=httpx.Timeout(UPSTREAM_TIMEOUT, connect=3.0))
    return _client


def _lookup() -> str | None:
    """running 상태인 우리 ASG 워커의 **사설** IP. 없으면 None."""
    if not ASG_NAME:
        return os.getenv("GPU_HOST") or None      # 로컬 개발 탈출구
    r = _ec2_client().describe_instances(Filters=[
        {"Name": "instance-state-name", "Values": ["running"]},
        {"Name": "tag:aws:autoscaling:groupName", "Values": [ASG_NAME]},
        {"Name": "tag:Project", "Values": [PROJECT]},
    ])
    for res in r.get("Reservations", []):
        for inst in res.get("Instances", []):
            # 공인 IP 가 아니라 사설 IP 다. 같은 VPC 안이라 사설로 닿고,
            # 공인으로 가면 트래픽이 NAT 를 돌아 데이터 전송 요금이 붙는다.
            ip = inst.get("PrivateIpAddress")
            if ip:
                return ip
    return None


def worker_ip() -> str | None:
    """캐시된 워커 IP. 조회 실패는 '없음'으로 본다 — 없으면 503 이 맞다."""
    global _cache
    now = time.time()
    with _lock:
        exp, ip = _cache
        if now < exp:
            return ip
    try:
        ip = _lookup()
    except Exception as e:
        print(f"[gpuproxy] 워커 조회 실패: {type(e).__name__}: {e}")
        ip = None
    with _lock:
        _cache = (now + (HIT_TTL if ip else MISS_TTL), ip)
        # 워커가 사라졌다 — 다음에 뜨는 놈은 새 프로세스라 다시 워밍이 필요하다.
        # IP 는 재사용될 수 있으므로 "본 적 있는 IP"를 영구히 들고 있으면 안 된다.
        if ip is None:
            _warmed.clear()
    return ip


def invalidate():
    """프록시가 연결에 실패했을 때. 죽은 IP 를 캐시 수명만큼 더 붙들지 않는다."""
    global _cache
    with _lock:
        _cache = (0.0, None)


def probe(ip: str) -> dict | None:
    """
    워커 :8001/health. 뜨는 중이면 연결 자체가 거부되므로 None 이다.

    짧게 끊는다(2초). 이 호출은 "준비됐나?"를 묻는 폴링 경로에 있어서,
    여기서 오래 붙들면 준비 안 된 상태를 확인하는 데 매번 그만큼 걸린다.
    """
    try:
        r = httpx.get(f"http://{ip}:{GPU_PORT}/health", timeout=2.0)
        if r.status_code != 200:
            return None
        health = r.json()
        maybe_warm(ip, health)
        return health
    except Exception:
        return None


def ready(health: dict | None) -> bool:
    """백엔드가 요청을 받을 수 있는 상태인가. compose 헬스체크와 같은 기준이다.

    **sam2 를 보면 안 된다.** sam2/lama 는 residency 가 유휴 600초에 반납하는
    모델이라, 멀쩡히 살아 있는 백엔드가 그 순간 "준비 안 됨"으로 뒤집힌다.
    그런데 그 둘을 다시 올리는 유일한 길이 /api/segment/mask 요청이고
    (segment.py 의 res.use('sam2') -> residency.ensure), 그 요청을 여기서
    막는다. 재로드를 트리거하는 유일한 요청을 재로드가 안 됐다는 이유로
    막으니, 한번 빠지면 브라우저가 몇 번을 재시도해도 영원히 못 나온다.

    2026-10-08 에 실제로 걸렸다. IDLE_EXIT_SEC 을 120 -> 900 으로 늘리면서
    인스턴스가 유휴 600초를 처음으로 넘겨 살아남았고, 그때 드러났다.
    900 이 버그를 만든 게 아니라 가려져 있던 걸 꺼냈을 뿐이다.

    extract 를 본다. lifespan 에서 sam2/lama **다음에** 올라가고
    (sam3d/backend/main.py 의 ExtractService), residency 의 반납 대상이
    아니라서, "기동이 끝났고 아직 살아 있다"를 정확히 뜻하는 유일한 값이다.
    """
    return bool(health) and health.get("extract") == "loaded"


# ── 첫 추론 워밍업 ──────────────────────────────────────────────────────
# SAM2 의 첫 추론은 CUDA 커널 컴파일 때문에 35.27초가 걸린다(2026-10-08 실측).
# 모델 로딩(1.9초)과는 다른 비용이고, **프로세스당 한 번**만 낸다 — TTL 로
# 모델을 내렸다 다시 올려도 다시 내지 않는다(같은 날 2차 측정 2.17초).
#
# 그 한 번을 사용자가 내면 첫 클릭이 35초다. UPSTREAM_TIMEOUT 이 25초이던
# 때는 503 으로 끝나 "마스크 로딩 중에서 안 끝남"으로 보였다. 지금은 50초라
# 끝나기는 하지만, 클릭 한 번에 35초를 기다리는 건 여전히 못 쓸 경험이다.
# 그래서 워커가 준비되는 순간 우리가 대신 한 번 낸다.
#
# SD 에도 같은 성질의 비용이 있다(첫 33.73초 → 이후 22.39초). 그쪽은 데우지
# 않기로 했다 — 첫 인페인팅 1회가 느릴 뿐이고(2026-10-08 실측 43.55초) 50초
# 안에 끝나며, 워밍업을
# 늘리면 GPU 가동 시간과 코드가 같이 늘기 때문이다(2026-10-08 결정).
#
# 올바른 자리는 백엔드의 lifespan(sd_warm 옆)이다. 거기는 AMI 안이라 고치려면
# 재빌드가 필요해서 여기에 둔다. 다음 AMI 를 구울 때 옮기고 이건 걷어낸다.
WARM = os.getenv("EMR_GPU_WARM", "1") != "0"
WARM_TIMEOUT = float(os.getenv("EMR_GPU_WARM_TIMEOUT", "180"))

# 64x64 PNG. SAM2 가 내부에서 1024 로 리사이즈하므로 크기는 중요하지 않다.
# 이미지를 코드에 박는 이유는 API 이미지에 PIL 이 없어서다 — 의존성을 하나
# 늘리느니 166바이트를 박는 게 싸다.
_WARM_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAbUlEQVR42u3ZsQ3A"
    "IAwEQIIYjSJjeSwKhmMCChcRIrrvLflkd/9ERLk5tVweAAAAAAAAAAAAgHNp2YF3"
    "zq93Gr17IQAAAAAAAAAAAAAAAAAAAAAAAAAAAACAfdIFR6p9cAEAAAAAAAAAAIBf"
    "AxZrRAZEbRuF9wAAAABJRU5ErkJggg==")

_warmed: set[str] = set()


def _warm(ip: str):
    t0 = time.time()
    try:
        # 전용 타임아웃이다. 공용 클라이언트의 UPSTREAM_TIMEOUT 을 쓰면 워밍업
        # 자신이 거기서 끊겨 목적을 못 이룬다(최초 추론이 37초대라 아슬아슬하다).
        # 사용자 요청 경로의 한도는 건드리지 않는다.
        r = httpx.post(f"http://{ip}:{GPU_PORT}/api/segment/mask",
                       files={"image": ("warm.png", _WARM_PNG, "image/png")},
                       data={"points": "[[32, 32]]"},
                       timeout=WARM_TIMEOUT)
        print(f"[gpuproxy] 워밍업 {ip} → {r.status_code} ({time.time()-t0:.1f}초)")
    except Exception as e:
        # 실패해도 아무것도 하지 않는다. 워밍업은 **빠르게 하는 것**이지
        # 가능하게 하는 게 아니다 — 실패하면 첫 클릭이 전처럼 느릴 뿐이고,
        # 그건 이 코드가 없던 때와 같은 상태다.
        print(f"[gpuproxy] 워밍업 {ip} 실패({type(e).__name__}: {e}) — 무시한다")


def maybe_warm(ip: str, health: dict | None):
    """워커가 처음 준비된 순간 딱 한 번, 백그라운드로 더미 추론을 보낸다.

    호출자(probe)는 결과를 기다리지 않는다. 폴링 경로에 있는 함수라 여기서
    붙들면 /api/gpu/status 가 통째로 35초 느려진다.
    """
    if not WARM or not ready(health):
        return
    with _lock:
        if ip in _warmed:
            return
        _warmed.add(ip)
    threading.Thread(target=_warm, args=(ip,), daemon=True).start()


def forward(method: str, path: str, *, content: bytes, headers: dict,
            params) -> httpx.Response:
    """
    워커로 그대로 넘긴다. 호출 쪽이 worker_ip() 로 이미 IP 를 확인한 뒤 부른다.

    헤더는 전부 넘기지 않는다. host 는 상류를 혼동시키고, content-length 는
    httpx 가 다시 계산한다. 그 둘만 빼면 나머지(content-type 의 multipart
    boundary 가 특히 중요하다)는 손대지 않는 게 맞다.
    """
    ip = worker_ip()
    if ip is None:
        raise RuntimeError("worker gone")
    fwd = {k: v for k, v in headers.items()
           if k.lower() not in ("host", "content-length", "connection")}
    return _http().request(
        method, f"http://{ip}:{GPU_PORT}/{path.lstrip('/')}",
        content=content, headers=fwd, params=params)
