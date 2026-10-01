"""
GPU 워커. SQS를 롱폴링하며 작업을 하나씩 꺼내 처리한다.

MOCK=1 이면 실제 추론 없이 더미 결과를 만든다. 1단계에서는 이 모드로
큐 배관만 검증하고, 3단계에서 run_inference()에 실제 모델을 붙인다.
GPU 없이 배관을 먼저 끝내야 디버깅 비용이 안 든다.

동시성 원칙: 이 루프는 한 번에 메시지 1건만 받는다(MaxNumberOfMessages=1).
SAM3D 파이프라인을 캐시해두면 그것만으로 **19.6GB**를 쓴다(4090 실측). 한
프로세스가 두 작업을 병렬로 돌릴 여지가 없다. 처리량은 워커 '대수'로 늘린다.
카드 크기를 따질 때 주의: g5.xlarge의 A10G는 "24GB"로 소개되지만 nvidia-smi
총량은 23,028 MiB로 개발용 4090(24,564)보다 1,536 MiB 적다.
"""
import json
import os
import signal
import threading
import time
import traceback

import boto3
from botocore.config import Config

ENDPOINT   = os.getenv("AWS_ENDPOINT_URL") or None
REGION     = os.getenv("AWS_REGION", "ap-northeast-2")
BUCKET     = os.getenv("S3_BUCKET", "emr-jobs")
TABLE_NAME = os.getenv("DDB_TABLE", "emr-jobs")
QUEUE_NAME = os.getenv("QUEUE_NAME", "emr-sam3d")
MOCK       = os.getenv("MOCK", "0") == "1"

# SAM3D 추론 모듈이 있는 디렉터리. 컨테이너 안에서도 호스트와 같은 절대경로로
# 마운트한다(conda prefix와 editable 설치가 경로에 박혀 있어서 옮길 수 없다).
# 기본값에 특정 사람의 홈 경로를 박아두지 않는다 — 그 PC 밖에서는 전부 틀린다.
# EMR_ROOT는 이미지에 구워져 있고(gpu/Dockerfile), compose가 다시 덮어쓴다.
EMR_ROOT    = os.getenv("EMR_ROOT", os.path.expanduser("~"))
BACKEND_DIR = os.getenv(
    "SAM3D_BACKEND_DIR", os.path.join(EMR_ROOT, "Empty_My_Room/sam3d/backend")
)

# 사이드카 주소. 로컬에서는 localhost, 컴포즈/운영에서는 서비스 이름이 들어온다.
ULAYOUT_URL = os.getenv("ULAYOUT_URL", "http://localhost:8002")
OMNI3D_URL  = os.getenv("OMNI3D_URL",  "http://localhost:8003")
SIDECAR_TIMEOUT = int(os.getenv("SIDECAR_TIMEOUT", "120"))

# 유휴 종료: 이 시간 동안 작업이 없으면 프로세스를 끝낸다.
# 0 이하면 스스로 끝내지 않는다 — ①(세 컨테이너 한 대) 구성의 기본값이다.
# 그 구성에서는 한 인스턴스에 워커가 둘이라, 한쪽이 마음대로 프로세스를 끝내면
# 재시작 루프만 돌 뿐 인스턴스는 안 내려간다. 종료 판단은 리퍼가 모아서 한다.
IDLE_EXIT_SEC = int(os.getenv("IDLE_EXIT_SEC", "900"))  # 15분

# 인스턴스 단위 스케일투제로용 상태 공유 디렉터리(리퍼와 함께 마운트한다).
# 비어 있으면 상태를 안 쓴다 — 리퍼 없이 단독으로 돌릴 때의 기본값.
STATE_DIR  = os.getenv("STATE_DIR", "")
DRAIN_FILE = os.path.join(STATE_DIR, "DRAIN") if STATE_DIR else ""

_cfg = Config(region_name=REGION, retries={"max_attempts": 3, "mode": "standard"})
s3  = boto3.client("s3",         endpoint_url=ENDPOINT, config=_cfg)
sqs = boto3.client("sqs",        endpoint_url=ENDPOINT, config=_cfg)
ddb = boto3.resource("dynamodb", endpoint_url=ENDPOINT, config=_cfg)
table = ddb.Table(TABLE_NAME)

_shutdown = threading.Event()


def _on_signal(signum, _frame):
    """
    SIGTERM 처리. 스팟 인스턴스 회수 시 AWS가 2분 전에 이 신호를 보낸다.
    지금 처리 중인 작업만 끝내고 새 작업은 받지 않는다(graceful shutdown).
    """
    print(f"[worker] 종료 신호({signum}) 수신 — 현재 작업 마치고 종료합니다")
    _shutdown.set()


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def publish_state(busy: bool):
    """
    내 상태를 파일 하나로 알린다. 리퍼(idle_reaper.py)가 이걸 읽어서
    "이 인스턴스의 워커가 전부 놀고 있는가"를 판단한다.

    왜 파일인가? 같은 인스턴스 안이라 네트워크를 쓸 이유가 없고, 컨테이너가
    죽어도 마지막 상태와 mtime이 남아서 리퍼가 "응답이 끊겼다"를 구분할 수 있다.
    임시 파일에 쓰고 rename 으로 갈아끼운다 — 그래야 리퍼가 반쯤 쓰인 JSON을
    읽고 죽는 일이 없다(rename은 같은 파일시스템에서 원자적이다).
    """
    if not STATE_DIR:
        return
    path = os.path.join(STATE_DIR, f"{QUEUE_NAME}.state")
    body = json.dumps({"queue": QUEUE_NAME, "busy": bool(busy),
                       "updated_at": int(time.time()), "pid": os.getpid()})
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            f.write(body)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[worker] 상태 파일 기록 실패(무시): {e}")


def draining() -> bool:
    """
    리퍼가 DRAIN 파일을 만들면 새 작업을 그만 받는다.

    스팟 회수 통지(2분 전)를 받았을 때 쓴다. 도커가 SIGTERM을 보내는 건 실제
    종료 직전이라, 그때까지 새 SAM3D 작업(30초)을 계속 집어가면 중간에 잘린다.
    잘린 작업이 사라지진 않지만(가시성 타임아웃이 지나면 큐로 돌아온다) 그만큼
    GPU 시간을 버리는 셈이라, 미리 받기를 멈추는 편이 싸다.
    """
    return bool(DRAIN_FILE) and os.path.exists(DRAIN_FILE)


def set_status(job_id: str, status: str, **extra):
    """DynamoDB 상태 갱신. 프런트엔드 폴링이 이 값을 읽는다."""
    names, values, sets = {"#s": "status"}, {":s": status, ":u": int(time.time())}, []
    sets.append("#s = :s")
    sets.append("updated_at = :u")
    for i, (k, v) in enumerate(extra.items()):
        names[f"#k{i}"] = k
        values[f":v{i}"] = v
        sets.append(f"#k{i} = :v{i}")
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def heartbeat(queue_url: str, receipt: str, stop: threading.Event):
    """
    가시성 타임아웃 연장 — 주니어가 가장 많이 걸려 넘어지는 부분이다.

    SQS는 메시지를 꺼내간 뒤 일정 시간(가시성 타임아웃) 안에 삭제되지 않으면
    '워커가 죽었다'고 보고 메시지를 큐에 되돌린다. SAM3D는 50초가 걸리는데
    기본값은 30초라, 그냥 두면 작업이 끝나기도 전에 다른 워커가 같은 작업을
    또 집어간다. 같은 메쉬를 두 번 만들고 GPU 비용도 두 배로 나간다.

    그래서 처리하는 동안 백그라운드에서 계속 타임아웃을 밀어준다.
    """
    while not stop.wait(30):
        try:
            sqs.change_message_visibility(
                QueueUrl=queue_url, ReceiptHandle=receipt, VisibilityTimeout=120
            )
        except Exception as e:
            print(f"[worker] 하트비트 실패(무시): {e}")
            return


# ── 실제 추론 ─────────────────────────────────────────────────────────────
#
# 작업 세 종류의 성격이 완전히 다르다.
#
#   sam3d_mesh  : 무거운 GPU 추론(25초). 이 프로세스 안에서 직접 돌린다.
#   room_layout : uLayout 사이드카로 HTTP 전달.
#   omni3d      : Omni3D 사이드카로 HTTP 전달.
#
# 왜 뒤의 둘은 직접 안 돌리는가? conda 환경이 서로 다르기 때문이다. uLayout과
# Omni3D는 torch/pytorch3d 버전이 충돌해서 한 파이썬 프로세스에 같이 올릴 수 없다.
# 그래서 원래도 별도 서버(:8002, :8003)로 띄워 HTTP로 부르고 있었고, 워커도
# 그 구조를 그대로 쓴다. 워커는 "큐에서 꺼내 사이드카에 넘기고 결과를 S3에 올리는"
# 역할만 한다.

_pipeline = None  # SAM3D 파이프라인 캐시(프로세스 수명 동안 유지)


def _get_pipeline():
    """
    SAM3D 파이프라인을 한 번만 로드해서 재사용한다.

    로드 자체가 수십 초 걸리므로 매 작업마다 다시 만들면 추론(25초)보다 준비가
    더 오래 걸린다. 워커가 살아 있는 동안 캐시해두면 두 번째 작업부터는 바로 돈다.
    스케일투제로로 인스턴스가 내려가면 캐시도 같이 사라지는데, 그게 콜드스타트의
    실체다(4단계에서 이 비용과 유휴 비용을 저울질한다).
    """
    global _pipeline
    if _pipeline is None:
        import sys
        if BACKEND_DIR not in sys.path:
            sys.path.insert(0, BACKEND_DIR)
        from services import sam3d_runner
        print("[worker] SAM3D 파이프라인 로드 중...")
        t0 = time.time()
        _pipeline = sam3d_runner.load_pipeline()
        print(f"[worker] SAM3D 파이프라인 로드 완료 ({time.time() - t0:.1f}초)")
    return _pipeline


def _drop_pipeline():
    """추론이 실패하면 캐시를 버린다 — 반쯤 망가진 GPU 상태를 다음 작업에 물려주지 않기 위함."""
    global _pipeline
    _pipeline = None
    try:
        import gc
        import torch
        gc.collect()
        torch.cuda.empty_cache()
        print("[worker] GPU 캐시 정리 완료")
    except Exception as e:
        print(f"[worker] GPU 캐시 정리 실패(무시): {e}")


def _run_sam3d_mesh(job, local_input, local_inputs):
    import sys
    if BACKEND_DIR not in sys.path:
        sys.path.insert(0, BACKEND_DIR)
    from services import sam3d_runner

    with open(local_input, "rb") as f:
        img_bytes = f.read()
    category = (job.get("params") or {}).get("category", "")

    pipeline = _get_pipeline()
    try:
        payload = sam3d_runner.generate(pipeline, img_bytes, category)
    except Exception:
        _drop_pipeline()
        raise

    import orjson
    return orjson.dumps(payload), "application/json"


def _post_sidecar(url: str, local_input: str, data: dict):
    """사이드카에 이미지 + 폼 데이터를 보내고 JSON 응답 바이트를 그대로 돌려준다."""
    import requests

    with open(local_input, "rb") as f:
        files = {"image": ("input.png", f.read(), "image/png")}
    resp = requests.post(url, files=files, data=data, timeout=SIDECAR_TIMEOUT)
    resp.raise_for_status()
    # 사이드카 JSON을 가공 없이 그대로 올린다. 프런트엔드가 동기 호출 때 받던
    # 응답과 한 글자도 다르지 않아야 결과 처리 코드를 손대지 않아도 된다.
    return resp.content, "application/json"


def _run_room_layout(job, local_input, local_inputs):
    params = job.get("params") or {}
    return _post_sidecar(
        f"{ULAYOUT_URL}/infer",
        local_input,
        {"camera_height_m": params.get("camera_height_m", 1.6)},
    )


def _run_omni3d(job, local_input, local_inputs):
    params = job.get("params") or {}
    missing = [k for k in ("bbox", "category") if k not in params]
    if missing:
        raise ValueError(f"omni3d 작업에 필요한 파라미터 누락: {missing}")
    return _post_sidecar(
        f"{OMNI3D_URL}/estimate",
        local_input,
        {"bbox": params["bbox"], "category": params["category"]},
    )


HANDLERS = {
    "sam3d_mesh":  _run_sam3d_mesh,
    "room_layout": _run_room_layout,
    "omni3d":      _run_omni3d,
}


def run_inference(job: dict, local_input: str, local_inputs: dict | None = None) -> tuple[bytes, str]:
    """
    실제 추론. 반환값은 (결과 바이트, content-type).

    local_input은 주 입력(image), local_inputs는 파트 이름 → 경로 전체.
    """
    job_type = job["job_type"]

    if MOCK:
        print(f"[worker] MOCK 처리 중... (job_type={job_type})")
        time.sleep(float(os.getenv("MOCK_SEC", "5")))
        dummy = {
            "success": True,
            "mock": True,
            "job_type": job_type,
            "mesh": {"vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "faces": [[0, 1, 2]]},
        }
        return json.dumps(dummy).encode(), "application/json"

    handler = HANDLERS.get(job_type)
    if handler is None:
        raise ValueError(f"처리할 수 없는 job_type: {job_type}")
    return handler(job, local_input, local_inputs or {"image": local_input})


def process(queue_url: str, msg: dict):
    job = json.loads(msg["Body"])
    job_id = job["job_id"]
    receipt = msg["ReceiptHandle"]
    print(f"[worker] 작업 시작 job_id={job_id} type={job['job_type']}")

    stop_hb = threading.Event()
    hb = threading.Thread(target=heartbeat, args=(queue_url, receipt, stop_hb), daemon=True)
    hb.start()
    publish_state(busy=True)

    try:
        set_status(job_id, "running")

        # 파트 이름 → 로컬 경로. SAM3D 얇은 가구 경로처럼 입력이 여러 개인
        # 작업이 있어서, 주 입력만이 아니라 온 것을 전부 내려받는다.
        input_keys = job.get("input_keys") or {"image": job["input_key"]}
        local_inputs = {}
        for field, key in input_keys.items():
            path = f"/tmp/{job_id}.{field}"
            s3.download_file(BUCKET, key, path)
            local_inputs[field] = path
        local_input = local_inputs["image"]

        t0 = time.time()
        payload, content_type = run_inference(job, local_input, local_inputs)
        elapsed = time.time() - t0

        result_key = f"result/{job_id}/result.json"
        s3.put_object(Bucket=BUCKET, Key=result_key, Body=payload, ContentType=content_type)
        set_status(job_id, "done", result_key=result_key, elapsed_sec=int(elapsed))

        # 성공했을 때만 메시지를 지운다. 이 순서가 중요하다 — 먼저 지우면
        # 결과 업로드가 실패했을 때 작업이 영영 사라진다.
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
        print(f"[worker] 완료 job_id={job_id} ({elapsed:.1f}초)")

    except Exception as e:
        traceback.print_exc()
        set_status(job_id, "failed", error=str(e)[:500])
        # 메시지를 지우지 않는다 → 가시성 타임아웃이 지나면 자동 재시도되고,
        # 지정 횟수를 넘기면 DLQ로 넘어간다. 삭제해버리면 원인 분석이 불가능해진다.
        print(f"[worker] 실패 job_id={job_id}: {e}")
    finally:
        stop_hb.set()
        publish_state(busy=False)


def resolve_queue_url(retries: int = 30, delay: float = 2.0) -> str:
    """
    큐 URL 조회를 재시도한다.

    기동 직후에는 큐가 아직 안 보일 수 있다(로컬은 LocalStack 초기화 중,
    운영은 Terraform 적용 직후나 일시적 네트워크 오류). 여기서 예외를 그대로
    터뜨리면 컨테이너가 죽고, ASG가 새 인스턴스를 띄우는 루프에 빠진다.
    """
    for i in range(retries):
        try:
            return sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]
        except sqs.exceptions.QueueDoesNotExist:
            print(f"[worker] 큐 '{QUEUE_NAME}' 대기 중... ({i+1}/{retries})")
        except Exception as e:
            print(f"[worker] 큐 조회 실패({type(e).__name__}) 재시도... ({i+1}/{retries})")
        time.sleep(delay)
    raise RuntimeError(f"큐 '{QUEUE_NAME}'를 {retries}회 시도했으나 찾지 못했습니다")


def main():
    queue_url = resolve_queue_url()
    idle_desc = f"{IDLE_EXIT_SEC}초" if IDLE_EXIT_SEC > 0 else "안 함(리퍼가 판단)"
    print(f"[worker] 시작 queue={QUEUE_NAME} mock={MOCK} 자가유휴종료={idle_desc}")
    last_work = time.time()
    publish_state(busy=False)   # 리퍼에게 "나 떴고 지금은 논다"를 먼저 알린다

    while not _shutdown.is_set():
        if draining():
            print("[worker] DRAIN 감지 — 새 작업을 받지 않고 종료합니다")
            break

        # 롱폴링(WaitTimeSeconds=20): 메시지가 없으면 최대 20초 기다렸다 응답한다.
        # 짧은 폴링으로 계속 두드리면 SQS 요청 수가 수백 배로 늘어 요금이 붙는다.
        resp = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,   # VRAM 보호 — 한 번에 하나만
            WaitTimeSeconds=20,
        )
        msgs = resp.get("Messages", [])
        if not msgs:
            # 놀고 있어도 주기적으로 상태를 갱신한다. 리퍼는 파일 mtime이 오래되면
            # "워커가 멎었다"고 보고 종료를 보류하므로, 갱신을 멈추면 안 내려간다.
            publish_state(busy=False)
            if IDLE_EXIT_SEC > 0 and time.time() - last_work > IDLE_EXIT_SEC:
                print(f"[worker] {IDLE_EXIT_SEC}초 동안 작업 없음 — 종료(스케일투제로)")
                break
            continue

        process(queue_url, msgs[0])
        last_work = time.time()

    print("[worker] 정상 종료")


if __name__ == "__main__":
    main()
