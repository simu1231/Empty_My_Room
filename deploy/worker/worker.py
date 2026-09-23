"""
GPU 워커. SQS를 롱폴링하며 작업을 하나씩 꺼내 처리한다.

MOCK=1 이면 실제 추론 없이 더미 결과를 만든다. 1단계에서는 이 모드로
큐 배관만 검증하고, 3단계에서 run_inference()에 실제 모델을 붙인다.
GPU 없이 배관을 먼저 끝내야 디버깅 비용이 안 든다.

동시성 원칙: 이 루프는 한 번에 메시지 1건만 받는다(MaxNumberOfMessages=1).
GPU VRAM이 24GB뿐이고 SAM3D 파이프라인 하나가 13GB를 쓰므로, 한 프로세스가
두 작업을 병렬로 돌리면 OOM이 난다. 처리량은 워커 '대수'로 늘린다.
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

# 유휴 종료: 이 시간 동안 작업이 없으면 프로세스를 끝낸다.
# 스케일투제로의 실제 구현부다. 4단계에서 ASG가 이 종료를 감지해 인스턴스를 회수한다.
IDLE_EXIT_SEC = int(os.getenv("IDLE_EXIT_SEC", "900"))  # 15분

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


def run_inference(job: dict, local_input: str) -> tuple[bytes, str]:
    """
    실제 추론이 들어갈 자리. 반환값은 (결과 바이트, content-type).

    3단계에서 여기에 기존 sam3d/backend/routers/sam3d.py의 추론 코드를 옮긴다.
    지금은 배관 검증용 더미다.
    """
    if MOCK:
        print(f"[worker] MOCK 처리 중... (job_type={job['job_type']})")
        time.sleep(float(os.getenv("MOCK_SEC", "5")))
        dummy = {
            "success": True,
            "mock": True,
            "job_type": job["job_type"],
            "mesh": {"vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "faces": [[0, 1, 2]]},
        }
        return json.dumps(dummy).encode(), "application/json"

    raise NotImplementedError("3단계에서 실제 모델을 연결합니다")


def process(queue_url: str, msg: dict):
    job = json.loads(msg["Body"])
    job_id = job["job_id"]
    receipt = msg["ReceiptHandle"]
    print(f"[worker] 작업 시작 job_id={job_id} type={job['job_type']}")

    stop_hb = threading.Event()
    hb = threading.Thread(target=heartbeat, args=(queue_url, receipt, stop_hb), daemon=True)
    hb.start()

    try:
        set_status(job_id, "running")

        local_input = f"/tmp/{job_id}"
        s3.download_file(BUCKET, job["input_key"], local_input)

        t0 = time.time()
        payload, content_type = run_inference(job, local_input)
        elapsed = time.time() - t0

        result_key = f"result/{job_id}/mesh.json"
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
    print(f"[worker] 시작 queue={QUEUE_NAME} mock={MOCK} 유휴종료={IDLE_EXIT_SEC}초")
    last_work = time.time()

    while not _shutdown.is_set():
        # 롱폴링(WaitTimeSeconds=20): 메시지가 없으면 최대 20초 기다렸다 응답한다.
        # 짧은 폴링으로 계속 두드리면 SQS 요청 수가 수백 배로 늘어 요금이 붙는다.
        resp = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,   # VRAM 보호 — 한 번에 하나만
            WaitTimeSeconds=20,
        )
        msgs = resp.get("Messages", [])
        if not msgs:
            if time.time() - last_work > IDLE_EXIT_SEC:
                print(f"[worker] {IDLE_EXIT_SEC}초 동안 작업 없음 — 종료(스케일투제로)")
                break
            continue

        process(queue_url, msgs[0])
        last_work = time.time()

    print("[worker] 정상 종료")


if __name__ == "__main__":
    main()
