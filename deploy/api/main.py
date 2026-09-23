"""
접수 전용 API 서버 (CPU 전용, 상시 가동).

GPU 워커가 0대일 때도 이 서버만은 항상 떠 있어야 한다. 사용자의 요청을
받아줄 곳이 없으면 스케일투제로 자체가 성립하지 않기 때문이다.

역할은 3가지뿐이다.
  1. 접수  : 이미지를 S3에 올리고 SQS에 메시지를 넣은 뒤 job_id를 즉시 반환
  2. 조회  : DynamoDB에서 작업 상태를 읽어 프런트엔드에 알려줌
  3. 전달  : 완료된 결과의 presigned URL 발급

추론은 절대 여기서 하지 않는다. torch를 import하는 순간 이미지가 수 GB로
불어나고, 상시 가동이라 그 비용이 매달 그대로 나간다.
"""
import json
import os
import time
import uuid

import boto3
from botocore.config import Config
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

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
async def create_job(
    image: UploadFile = File(...),
    job_type: str = Form(...),
    params: str = Form("{}"),
):
    """작업을 접수하고 즉시 job_id를 돌려준다. 여기서 절대 기다리지 않는다."""
    try:
        parsed_params = json.loads(params)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"params가 올바른 JSON이 아닙니다: {e}")

    job_id = uuid.uuid4().hex
    now = int(time.time())
    input_key = f"input/{job_id}/{image.filename or 'image.png'}"

    # 이미지는 S3로. SQS 메시지에 직접 넣으면 안 된다(메시지 최대 256KB).
    body = await image.read()
    s3.put_object(
        Bucket=BUCKET, Key=input_key, Body=body,
        ContentType=image.content_type or "application/octet-stream",
    )

    # 상태 레코드를 먼저 쓴다. 큐에 먼저 넣으면 워커가 즉시 집어갔을 때
    # 조회할 레코드가 없어서 404가 날 수 있다(경쟁 상태).
    table().put_item(Item={
        "job_id": job_id,
        "status": "queued",
        "job_type": job_type,
        "input_key": input_key,
        "params": parsed_params,
        "created_at": now,
        "updated_at": now,
        # TTL: 7일 뒤 자동 삭제. 안 지우면 레코드가 무한히 쌓인다.
        "expires_at": now + 7 * 24 * 3600,
    })

    sqs.send_message(
        QueueUrl=queue_url(job_type),
        MessageBody=json.dumps({
            "job_id": job_id,
            "job_type": job_type,
            "input_key": input_key,
            "params": parsed_params,
        }),
    )

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
    return resp


@app.get("/health")
def health():
    return {"status": "ok"}
