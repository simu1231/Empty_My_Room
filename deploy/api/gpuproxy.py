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

# 상류 대기 한도. CloudFront 의 오리진 응답 타임아웃이 기본 30초이고, 그걸
# 넘기면 CloudFront 가 연결을 끊는다 — 그 상태는 프런트에서 502/504 로 보이고
# 원인을 알 수 없다. 우리가 25초에 먼저 포기하고 503 + Retry-After 로 바꿔
# 주면, 같은 상황이 "아직 준비 중"이라는 **해석 가능한** 응답이 된다.
UPSTREAM_TIMEOUT = float(os.getenv("GPU_UPSTREAM_TIMEOUT", "25"))

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
        return r.json()
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
