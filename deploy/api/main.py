"""
접수 전용 API 서버 (CPU 전용, 상시 가동).

GPU 워커가 0대일 때도 이 서버만은 항상 떠 있어야 한다. 사용자의 요청을
받아줄 곳이 없으면 스케일투제로 자체가 성립하지 않기 때문이다.

역할은 3가지뿐이다.
  1. 접수  : 이미지를 S3에 올리고 SQS에 메시지를 넣은 뒤 job_id를 즉시 반환
  2. 조회  : DynamoDB에서 작업 상태를 읽어 프런트엔드에 알려줌
  3. 전달  : 완료된 결과의 presigned URL 발급
  4. 중계  : 2단계(대화형 세그먼트/인페인팅) 요청을 GPU 워커 :8001 로 넘김
  5. 대역  : GPU 가 아직 없거나 준비 중이면 붙들지 않고 503 + Retry-After

4·5 가 뒤에 붙은 이유는 B'(CloudFront 단일 출처) 때문이다. 브라우저가 GPU
워커의 바뀌는 IP 를 직접 부르던 구조를 여기로 모으면, 워커의 8001 을
0.0.0.0/0 에 열 필요가 없어지고 CORS 도 통째로 사라진다(같은 출처가 된다).

추론은 절대 여기서 하지 않는다. torch를 import하는 순간 이미지가 수 GB로
불어나고, 상시 가동이라 그 비용이 매달 그대로 나간다.
"""
import json
import os
import time
import uuid

import boto3
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Request

import capacity
import colors
import gpuproxy
# 워커와 **같은** 검증 파일. Dockerfile 이 worker/jobspec.py 를 복사해 넣는다.
import jobspec
from starlette.datastructures import UploadFile as StarletteUploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

# ── 설정 ──────────────────────────────────────────────────────────────
# AWS_ENDPOINT_URL이 있으면 LocalStack(로컬), 없으면 실제 AWS를 본다.
# 코드를 한 줄도 바꾸지 않고 로컬/운영을 오갈 수 있게 하는 부분이다.
ENDPOINT   = os.getenv("AWS_ENDPOINT_URL") or None
REGION     = os.getenv("AWS_REGION", "ap-northeast-2")
BUCKET     = os.getenv("S3_BUCKET", "emr-jobs")
TABLE_NAME = os.getenv("DDB_TABLE", "emr-jobs")

# 작업 종류별로 큐를 나눈다. 종류마다 처리 시간과 필요 GPU가 달라서,
# 하나의 큐에 섞으면 오토스케일링 기준(큐 길이)이 의미를 잃는다.
QUEUE_NAMES = {
    "sam3d_mesh":  os.getenv("Q_SAM3D",  "emr-sam3d"),
    "room_layout": os.getenv("Q_SCENE",  "emr-scene"),
    "omni3d":      os.getenv("Q_SCENE",  "emr-scene"),
}

# 라우팅 표(어느 큐로 보낼까)와 검증 표(무엇이 유효한가)는 다른 관심사라
# 따로 둔다. 다만 둘이 어긋나면 "검증은 통과했는데 보낼 큐가 없는" 작업이
# 생기므로, 기동 시점에 한 번 맞춰 본다. 조용히 틀린 채 도는 것보다 낫다.
assert set(QUEUE_NAMES) == set(jobspec.JOB_TYPES), (
    f"QUEUE_NAMES {sorted(QUEUE_NAMES)} 와 jobspec.JOB_TYPES "
    f"{sorted(jobspec.JOB_TYPES)} 가 다릅니다")

# presigned URL은 "브라우저가 직접 여는 주소"다. 컨테이너 내부 주소로 서명하면
# API 서버끼리는 통하지만 사용자 브라우저에서는 열리지 않는다.
# 그래서 서명 전용 클라이언트를 따로 둔다. 운영(실제 S3)에서는 둘 다 None이라
# 같은 클라이언트가 되고, 로컬에서만 localhost 주소로 서명된다.
PUBLIC_ENDPOINT = os.getenv("AWS_PUBLIC_ENDPOINT_URL") or ENDPOINT

_cfg = Config(region_name=REGION, retries={"max_attempts": 3, "mode": "standard"})
s3  = boto3.client("s3",       endpoint_url=ENDPOINT, config=_cfg)
s3_public = (s3 if PUBLIC_ENDPOINT == ENDPOINT
             else boto3.client("s3", endpoint_url=PUBLIC_ENDPOINT, config=_cfg))
sqs = boto3.client("sqs",      endpoint_url=ENDPOINT, config=_cfg)
ddb = boto3.resource("dynamodb", endpoint_url=ENDPOINT, config=_cfg)

app = FastAPI(title="Empty My Room - Job API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_queue_url_cache: dict[str, str] = {}


def queue_url(job_type: str) -> str:
    """큐 이름 → URL. 매 요청마다 조회하면 느리므로 캐시한다."""
    name = QUEUE_NAMES.get(job_type)
    if not name:
        raise HTTPException(400, f"알 수 없는 job_type: {job_type}")
    if name not in _queue_url_cache:
        _queue_url_cache[name] = sqs.get_queue_url(QueueName=name)["QueueUrl"]
    return _queue_url_cache[name]


def table():
    return ddb.Table(TABLE_NAME)


# ── 1. 접수 ───────────────────────────────────────────────────────────
@app.post("/api/jobs")
async def create_job(request: Request):
    """
    작업을 접수하고 즉시 job_id를 돌려준다. 여기서 절대 기다리지 않는다.

    폼을 통째로 받아서 해석한다. 모델마다 필요한 입력이 달라서
    (SAM3D는 얇은 가구일 때 full_image+bbox를 더 받고, Omni3D는 bbox+category를
    받는다) 모델 수만큼 엔드포인트를 만들면 모델을 추가할 때마다 API 서버를
    다시 배포해야 한다. 파일 파트는 전부 S3로, 텍스트 파트는 전부 params로
    넘기면 API 서버는 무엇이 오든 그대로 워커에 전달하기만 하면 된다.
    """
    form = await request.form()

    job_type = form.get("job_type")
    if not isinstance(job_type, str) or job_type not in QUEUE_NAMES:
        raise HTTPException(400, f"알 수 없는 job_type: {job_type}")

    job_id = uuid.uuid4().hex
    now = int(time.time())

    input_keys: dict[str, str] = {}
    params: dict = {}

    # 2단으로 나눈다. 먼저 텍스트 파트만 모아 검증하고, 통과한 작업만 업로드한다.
    # 한 번에 하면 400 으로 거절할 작업의 이미지가 이미 S3 에 올라가 고아로 남는다.
    uploads: list = []
    for field, value in form.multi_items():
        if field == "job_type":
            continue
        if isinstance(value, StarletteUploadFile):
            uploads.append((field, value))
        elif field == "params":
            # 구조가 있는 값은 params에 JSON으로 한 번에 보낼 수도 있다.
            try:
                params.update(json.loads(value))
            except json.JSONDecodeError as e:
                raise HTTPException(400, f"params가 올바른 JSON이 아닙니다: {e}")
        else:
            params[field] = value

    # 워커와 **같은** 함수로 검증한다. 여기서 통과한 작업은 워커도 통과시킨다.
    # 규칙이 갈라지면 사용자는 접수 응답을 받고 기다렸다가 failed 를 본다.
    try:
        jobspec.validate(job_type, params, {f: "" for f, _ in uploads})
    except jobspec.InvalidJob as e:
        raise HTTPException(400, str(e))

    # 검증을 통과했으니 이제 올린다.
    # 이미지는 S3로. SQS 메시지에 직접 넣으면 안 된다(메시지 최대 256KB).
    for field, value in uploads:
        key = f"input/{job_id}/{field}/{value.filename or 'file'}"
        s3.put_object(
            Bucket=BUCKET, Key=key, Body=await value.read(),
            ContentType=value.content_type or "application/octet-stream",
        )
        input_keys[field] = key

    job_msg = {
        "job_id": job_id,
        "job_type": job_type,
        # input_key는 주 입력. 워커의 기존 코드와 호환을 위해 남긴다.
        "input_key": input_keys["image"],
        "input_keys": input_keys,
        "params": params,
    }

    # 상태 레코드를 먼저 쓴다. 큐에 먼저 넣으면 워커가 즉시 집어갔을 때
    # 조회할 레코드가 없어서 404가 날 수 있다(경쟁 상태).
    table().put_item(Item={
        **job_msg,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        # TTL: 7일 뒤 자동 삭제. 안 지우면 레코드가 무한히 쌓인다.
        "expires_at": now + 7 * 24 * 3600,
    })

    sqs.send_message(QueueUrl=queue_url(job_type), MessageBody=json.dumps(job_msg))

    # GPU 워커가 0대일 수 있다. 큐에 넣는 것만으로는 아무도 안 깨어난다 —
    # CloudWatch는 잠든 큐가 깨어날 때 최대 15분 늦으므로 여기서 직접 깨운다.
    # (자세한 이유는 capacity.py 주석)
    capacity.request_capacity()

    return {"job_id": job_id, "status": "queued"}


# ── 2. 조회 ───────────────────────────────────────────────────────────
@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    """프런트엔드가 2초 간격으로 부르는 엔드포인트. 가볍게 유지해야 한다."""
    item = table().get_item(Key={"job_id": job_id}).get("Item")
    if not item:
        raise HTTPException(404, "job을 찾을 수 없습니다")

    resp = {
        "job_id":   job_id,
        "status":   item["status"],
        "job_type": item.get("job_type"),
        "created_at": int(item.get("created_at", 0)),
    }
    if item["status"] == "done":
        # 결과는 API 서버를 거치지 않고 S3에서 직접 받게 한다.
        # 메쉬 파일이 수 MB라 API 서버로 중계하면 대역폭 낭비가 크다.
        resp["result_url"] = s3_public.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": item["result_key"]},
            ExpiresIn=3600,
        )
    elif item["status"] == "failed":
        resp["error"] = item.get("error", "알 수 없는 오류")
        # non_retryable 은 큐에서 즉시 지워져 DLQ 에 남지 않는다. DLQ 알람으로는
        # 영영 안 보이므로, 조회 응답이 유일한 흔적이다. 반드시 내보낸다.
        resp["failure_kind"] = item.get("failure_kind", "retryable")
    return resp


@app.get("/health")
def health():
    return {"status": "ok"}


# ── 4. GPU 중계 ───────────────────────────────────────────────────────
# 1단계(메쉬/레이아웃)는 큐를 거치지만, 2단계는 대화형이라 큐에 넣을 수 없다.
# 사용자가 마스크를 찍을 때마다 왕복 1~2초 안에 답이 와야 하기 때문이다.
# 그래서 2단계만 여기서 **동기 중계**한다.
#
# 핵심 규약: **GPU 가 없다고 요청을 붙들지 않는다.** 워커 부팅은 7분쯤
# 걸리는데 그동안 연결을 잡고 있으면 CloudFront(오리진 응답 30초)가 먼저
# 끊어 버리고, 프런트는 원인을 알 수 없는 502 를 본다. 대신 즉시 503 과
# Retry-After 를 주고 프런트가 되묻게 한다. 기다림이 **보이는** 기다림이 된다.

# 상태별 재시도 간격. 부팅은 길게(어차피 몇 분), 모델 적재는 중간,
# 상류 타임아웃은 짧게 — 이미 25초를 쓴 뒤라 사용자는 충분히 기다렸다.
RETRY_BOOT   = 20
RETRY_MODELS = 15
RETRY_BUSY   = 10

# 0대 → 첫 요청 처리 가능까지의 실측 근사치. 프런트가 진행 막대를 그리는 데만
# 쓴다. 틀려도 동작에는 영향이 없지만, 너무 짧게 잡으면 "곧 된다"고 해 놓고
# 안 되는 게 반복돼서 사용자가 새로고침한다.
ETA_BOOT   = 420
ETA_MODELS = 180
ETA_SD     = 120

# SD 선반입 중에도 막아야 하는 경로. 인페인팅만 SD 를 쓴다 — 세그먼트는
# SAM2 만 있으면 되므로 선반입이 끝나기 전에도 받아야 한다. 여기를 넓게
# 잡으면 선반입의 의미가 사라진다(세그먼트부터 막혀서 결국 기다린다).
SD_PATHS = ("inpaint",)


def _gpu_blocked(path: str) -> JSONResponse | None:
    """
    GPU 가 받을 수 없는 상태면 503 응답을, 받을 수 있으면 None 을 돌려준다.

    네 가지 상태를 구분한다. 프런트는 phase 를 그대로 문구로 쓴다 —
    "준비 중"이라고만 하면 7분을 설명할 수 없다.
    """
    ip = gpuproxy.worker_ip()
    if ip is None:
        # 0대. 여기서 깨우는 게 중요하다 — 큐를 거치지 않는 경로라 아무도
        # 안 깨운다. 중복 호출은 capacity.py 가 30초 창으로 막는다.
        capacity.request_capacity()
        return JSONResponse(status_code=503,
                            headers={"Retry-After": str(RETRY_BOOT)},
                            content={"state": "starting", "phase": "instance_boot",
                                     "eta_sec": ETA_BOOT,
                                     "message": "GPU 서버를 켜는 중입니다"})

    health = gpuproxy.probe(ip)
    if not gpuproxy.ready(health):
        return JSONResponse(status_code=503,
                            headers={"Retry-After": str(RETRY_MODELS)},
                            content={"state": "starting", "phase": "models",
                                     "eta_sec": ETA_MODELS,
                                     "message": "모델을 불러오는 중입니다"})

    # sd_warm: None=선반입 안 함 / False=진행 중 / True=완료.
    # None 이면 막지 않는다 — 선반입이 꺼진 환경(개발 PC)에서 인페인팅이
    # 영영 503 이 되면 안 된다.
    if any(p in path for p in SD_PATHS) and health.get("sd_warm") is False:
        return JSONResponse(status_code=503,
                            headers={"Retry-After": str(RETRY_MODELS)},
                            content={"state": "starting", "phase": "sd",
                                     "eta_sec": ETA_SD,
                                     "message": "인페인팅 모델을 준비하는 중입니다"})
    return None


@app.get("/api/gpu/status")
def gpu_status():
    """
    프런트가 진행 상태를 그리려고 부른다. 200 으로만 답한다 —
    이건 "준비됐나?"를 **묻는** 요청이지 처리를 요구하는 요청이 아니다.
    여기까지 503 으로 답하면 프런트가 재시도 루프를 두 겹으로 돌게 된다.
    """
    ip = gpuproxy.worker_ip()
    if ip is None:
        return {"state": "stopped", "phase": "none", "eta_sec": ETA_BOOT}
    health = gpuproxy.probe(ip)
    if not gpuproxy.ready(health):
        return {"state": "starting", "phase": "models", "eta_sec": ETA_MODELS}
    return {"state": "ready", "phase": "ready", "eta_sec": 0,
            "sd_warm": health.get("sd_warm")}


@app.api_route("/api/gpu/{path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def gpu_proxy(path: str, request: Request):
    blocked = _gpu_blocked(path)
    if blocked is not None:
        return blocked

    body = await request.body()
    try:
        r = gpuproxy.forward(request.method, path, content=body,
                             headers=dict(request.headers),
                             params=request.query_params)
    except Exception as e:
        # 여기 걸리는 건 둘 중 하나다. (a) 25초를 넘겼다 — 첫 SD 로드처럼
        # 원래 오래 걸리는 작업이거나, (b) 방금까지 있던 워커가 사라졌다
        # (스팟 회수). 둘 다 "다시 물어보라"가 맞는 답이고, 504 로 주면
        # CloudFront 가 자기 타임아웃과 섞어서 구분이 안 된다.
        gpuproxy.invalidate()
        print(f"[gpu] 중계 실패 {request.method} /{path}: {type(e).__name__}: {e}")
        return JSONResponse(status_code=503,
                            headers={"Retry-After": str(RETRY_BUSY)},
                            content={"state": "busy", "phase": "upstream",
                                     "eta_sec": RETRY_BUSY,
                                     "message": "GPU 서버가 아직 응답하지 않습니다"})

    # 상류 헤더를 그대로 흘리면 안 되는 것들이 있다. hop-by-hop 헤더는
    # 이 연결에서만 의미가 있고, content-length 는 Response 가 다시 센다.
    drop = {"content-length", "transfer-encoding", "connection",
            "keep-alive", "content-encoding"}
    return Response(content=r.content, status_code=r.status_code,
                    headers={k: v for k, v in r.headers.items()
                             if k.lower() not in drop},
                    media_type=r.headers.get("content-type"))


# ── 5. 선반입 ─────────────────────────────────────────────────────────
# 사용자가 사진을 **고른** 순간 GPU 를 깨운다. 그 뒤 사용자는 가구를 클릭해
# 마스크를 찍느라 최소 수십 초를 쓰는데, 그 시간이 부팅과 겹친다.
#
# 추가 GPU 가 뜰 위험은 없다. capacity.request_capacity() 는 desired>0 이고
# 살아 있는 인스턴스가 있으면 **그대로 반환한다** — 증설은 알람의 몫이다.
PREWARM_RATE_SEC = int(os.getenv("PREWARM_RATE", "30"))
_prewarm_seen: dict[str, float] = {}


@app.post("/api/prewarm")
def prewarm(request: Request):
    """
    202 로만 답한다. 성공/실패가 아니라 "접수했다"가 정확한 의미다 —
    실제로 깨어나는지는 /api/gpu/status 가 알려준다.

    IP 당 제한을 두는 이유: 이 엔드포인트는 인증이 없고 ASG 를 건드린다.
    누가 초당 수백 번 부르면 DescribeAutoScalingGroups 가 요청 제한에 걸려
    **정상 접수 경로까지 같이 막힌다.** capacity.py 의 30초 창은 전역이라
    호출 자체는 막지 못한다 — 들어오는 쪽에서도 한 겹 더 센다.
    """
    now = time.time()
    who = request.client.host if request.client else "?"
    # 메모리 누수 방지. 방문자가 많아도 창이 지난 항목은 바로 버린다.
    for k in [k for k, t in _prewarm_seen.items() if now - t > PREWARM_RATE_SEC]:
        _prewarm_seen.pop(k, None)
    if now - _prewarm_seen.get(who, 0.0) < PREWARM_RATE_SEC:
        return JSONResponse(status_code=202,
                            content={"state": "throttled", "eta_sec": ETA_BOOT})
    _prewarm_seen[who] = now

    capacity.request_capacity()
    ip = gpuproxy.worker_ip()
    return JSONResponse(status_code=202,
                        content={"state": "ready" if ip else "starting",
                                 "eta_sec": 0 if ip else ETA_BOOT})


# ── 6. 색 추출 (GPU 불필요) ───────────────────────────────────────────
@app.post("/api/extract-colors")
async def extract_colors(request: Request):
    """
    GPU 백엔드에 있던 것을 그대로 옮겨 왔다. 왜 옮겼는지는 colors.py 머리말에.
    프런트는 /api/gpu/extract-colors 가 아니라 여기를 부른다 — 이 요청 때문에
    GPU 가 깨어나면 안 되기 때문이다.
    """
    form = await request.form()
    up = form.get("image")
    if not isinstance(up, StarletteUploadFile):
        raise HTTPException(400, "image 파일이 필요합니다")
    data = await up.read()
    if not data:
        raise HTTPException(400, "빈 이미지입니다")
    try:
        return colors.extract(data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"이미지를 읽을 수 없습니다: {type(e).__name__}")
