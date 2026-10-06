"""
유휴 리퍼 — 인스턴스를 스스로 회수해서 스케일투제로를 완성한다.

■ 왜 ASG의 스케일인 정책을 안 쓰는가
ASG가 보는 건 큐 길이뿐이다. SAM3D가 30초짜리 작업을 처리하는 동안 그 메시지는
"처리 중(NotVisible)"이라 큐에서 안 보인다. 즉 **가장 바쁜 순간이 CloudWatch에는
가장 한가해 보인다.** 그 상태로 스케일인을 걸면 작업 중인 인스턴스를 골라 죽인다.
작업이 사라지진 않지만(메시지가 큐로 돌아온다) GPU 30초를 버리고 처음부터 다시 한다.

그래서 종료 판단은 "지금 일하는 중인지"를 유일하게 아는 쪽, 즉 인스턴스 자신이 한다.
ASG 쪽 타깃 추적 정책은 스케일인을 꺼두고(DisableScaleIn), 줄이는 건 전부 여기서 한다.

■ 왜 워커가 직접 안 죽고 리퍼를 따로 두는가
①(세 컨테이너 한 대) 구성에서는 한 인스턴스에 워커가 둘(sam3d, scene)이다.
sam3d 워커가 15분 놀았다고 인스턴스를 내리면, 바로 옆에서 scene 워커가 돌리던
작업이 같이 죽는다. 그래서 워커는 자기 상태를 파일로 알리기만 하고(publish_state),
"전부 놀고 있는가"는 인스턴스에 하나뿐인 이 프로세스가 모아서 판단한다.

■ 하는 일
  1. 워커 상태 파일을 모아 전원 유휴 시간을 잰다.
  2. IDLE_EXIT_SEC를 넘기면 큐를 한 번 더 확인하고(경합 방지)
     TerminateInstanceInAutoScalingGroup(ShouldDecrementDesiredCapacity=True)로
     자기 자신을 내린다. desired를 같이 깎아야 ASG가 대체 인스턴스를 안 띄운다.
  3. 스팟 회수 통지(IMDS)를 폴링하다 뜨면 DRAIN 파일을 만들어 워커가 새 작업을
     그만 받게 한다.

DRY_RUN=1 이면 종료 대신 로그만 찍는다. 로컬(LocalStack)에서 판단 로직만 볼 때 쓴다.
"""
import json
import os
import time

import boto3
import requests
from botocore.config import Config

ENDPOINT  = os.getenv("AWS_ENDPOINT_URL") or None
REGION    = os.getenv("AWS_REGION", "ap-northeast-2")
STATE_DIR = os.getenv("STATE_DIR", "/state")

# 이 인스턴스에 떠 있어야 할 워커들. 한 명이라도 상태 파일을 아직 안 냈으면
# (= 기동 중이면) 종료 판단을 보류한다. 그러지 않으면 부팅 직후, 워커가 모델을
# 올리는 동안 "아무도 안 바쁘다"로 읽혀 갓 뜬 인스턴스를 바로 죽인다.
EXPECT = [q.strip() for q in os.getenv("EXPECT_WORKERS", "emr-sam3d,emr-scene").split(",") if q.strip()]
QUEUES = [q.strip() for q in os.getenv("WATCH_QUEUES",   "emr-sam3d,emr-scene").split(",") if q.strip()]

# 그중 **있으면 듣고 없으면 넘어가는** 쪽. 대화형 백엔드가 여기 들어간다.
#
# 큐 워커와 비대칭인 이유. 큐 워커의 상태 파일이 없거나 멎었다는 건 "작업을
# 쥔 채 멎었을 수 있다"는 뜻이라 보류가 안전하다. 백엔드의 상태 파일은 그게
# 아니라 "사용자가 화면 앞에 있나"라는 **덤 정보**다. 없으면 모르는 것이고,
# 모르는 상태는 이 기능이 생기기 전과 똑같다 — 그때로 돌아가면 된다.
#
# 이 구분이 없으면 돈이 샌다. 상태 파일은 named volume 에 남으므로, 백엔드가
# 한 번 쓰고 죽으면 파일은 "응답없음"으로 남고 리퍼는 영원히 보류한다. 가디언도
# 못 잡는다 — 가디언이 보는 건 "리퍼가 살아있나"뿐이고 리퍼는 멀쩡히 살아서
# 보류하고 있기 때문이다. 빈 g6.xlarge 가 시간당 $0.45 로 계속 돈다.
OPTIONAL = set(q.strip() for q in os.getenv("OPTIONAL_WORKERS", "backend").split(",") if q.strip())

IDLE_EXIT_SEC = int(os.getenv("IDLE_EXIT_SEC", "120"))
POLL_SEC      = int(os.getenv("REAPER_POLL_SEC", "15"))
# 상태 파일이 이만큼 안 갱신되면 그 워커는 "죽었거나 멎었다"로 본다. 워커는 롱폴링
# 20초마다 갱신하므로 그 3배쯤이면 충분하다. 이 경우 종료하지 않고 보류한다 —
# 멎은 워커를 유휴로 오해해서 멀쩡한 작업과 함께 인스턴스를 내리는 게 더 나쁘다.
STALE_SEC     = int(os.getenv("REAPER_STALE_SEC", "90"))
DRY_RUN       = os.getenv("DRY_RUN", "0") == "1"

IMDS = "http://169.254.169.254"

_cfg = Config(region_name=REGION, retries={"max_attempts": 3, "mode": "standard"})
sqs = boto3.client("sqs", endpoint_url=ENDPOINT, config=_cfg)


# ── IMDS ────────────────────────────────────────────────────────────────
def _imds_token():
    """IMDSv2 토큰. v1은 기본으로 막혀 있는 계정이 많아서 v2로만 접근한다."""
    r = requests.put(f"{IMDS}/latest/api/token",
                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"}, timeout=2)
    r.raise_for_status()
    return r.text


def _imds_get(path, token):
    r = requests.get(f"{IMDS}{path}", headers={"X-aws-ec2-metadata-token": token}, timeout=2)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text


def instance_id():
    try:
        return _imds_get("/latest/meta-data/instance-id", _imds_token())
    except Exception:
        return None  # EC2가 아니다(로컬 도커) — DRY_RUN으로만 의미가 있다


def spot_interrupted():
    """
    스팟 회수 통지. 회수 2분 전부터 이 경로가 200을 돌려준다.
    평소에는 404라서, 404를 예외로 만들지 않는 게 중요하다.
    """
    try:
        return _imds_get("/latest/meta-data/spot/instance-action", _imds_token()) is not None
    except Exception:
        return False


# ── 상태 수집 ────────────────────────────────────────────────────────────
def read_states():
    """워커 상태 파일을 모두 읽는다. 반환: {queue: (busy, updated_at, age)}"""
    out = {}
    now = time.time()
    for q in EXPECT:
        path = os.path.join(STATE_DIR, f"{q}.state")
        try:
            with open(path) as f:
                d = json.load(f)
            out[q] = (bool(d.get("busy")), int(d.get("updated_at", 0)),
                      now - os.path.getmtime(path))
        except FileNotFoundError:
            out[q] = None
        except Exception as e:
            print(f"[reaper] {q} 상태 읽기 실패(보류로 처리): {e}")
            # 못 읽으면 바쁜 것으로 간주한다 — 필수 워커에게는 안전한 쪽이다.
            # 나이는 **실제 mtime** 으로 준다. 예전엔 0.0 을 박았는데, 그건
            # "방금 갱신됨"이라는 거짓말이라 OPTIONAL 쪽에서 영원한 보류가
            # 된다(아래 fresh_busy 가 나이를 본다). 필수 워커는 어차피
            # busy 로도 stale 로도 보류라 결과가 같다.
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                out[q] = None
                continue
            out[q] = (True, 0, age)
    return out


def queues_empty():
    """
    종료 직전 마지막 확인. 상태 파일이 "다 논다"여도, 바로 그 순간 큐에 메시지가
    들어왔을 수 있다. 워커가 그걸 집기 전에 인스턴스를 내리면 메시지는 큐에 남고
    스케일아웃 알람이 새 인스턴스를 띄우므로 데이터는 안 잃지만, 사용자 입장에선
    부팅 3분을 통째로 더 기다린다. 그 한 번을 줄이려고 여기서 한 번 더 본다.
    """
    for q in QUEUES:
        try:
            url = sqs.get_queue_url(QueueName=q)["QueueUrl"]
            a = sqs.get_queue_attributes(
                QueueUrl=url,
                AttributeNames=["ApproximateNumberOfMessages",
                                "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
            n = int(a["ApproximateNumberOfMessages"]) + int(a["ApproximateNumberOfMessagesNotVisible"])
            if n > 0:
                print(f"[reaper] 큐 {q}에 {n}건 남음 — 종료 보류")
                return False
        except Exception as e:
            print(f"[reaper] 큐 {q} 확인 실패(보류로 처리): {e}")
            return False
    return True


def set_drain(reason: str):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "DRAIN"), "w") as f:
            f.write(reason)
        print(f"[reaper] DRAIN 생성 — {reason}")
    except Exception as e:
        print(f"[reaper] DRAIN 생성 실패: {e}")


def terminate_self(iid: str):
    """
    자기 인스턴스를 ASG에서 뺀다.

    ShouldDecrementDesiredCapacity=True가 핵심이다. False로 두면 ASG는 desired를
    유지하려고 **즉시 새 인스턴스를 띄운다.** 스케일투제로가 무한 재기동 루프가
    되고 요금은 계속 나간다. 이 한 글자가 4단계 전체의 성패다.
    """
    asg = boto3.client("autoscaling", config=_cfg)
    g = asg.describe_auto_scaling_instances(InstanceIds=[iid])["AutoScalingInstances"]
    if not g:
        print(f"[reaper] {iid}는 ASG 소속이 아니다 — 종료하지 않는다")
        return
    name = g[0]["AutoScalingGroupName"]
    print(f"[reaper] ASG {name}에서 {iid} 종료 요청(desired -1)")
    asg.terminate_instance_in_auto_scaling_group(
        InstanceId=iid, ShouldDecrementDesiredCapacity=True)


def main():
    iid = instance_id()
    print(f"[reaper] 시작 instance={iid} 유휴기준={IDLE_EXIT_SEC}초 "
          f"대상워커={EXPECT} dry_run={DRY_RUN}")

    idle_since = None
    while True:
        if spot_interrupted():
            set_drain("spot-interruption")
            # 여기서 스스로 terminate 하지 않는다. 회수는 이미 확정이고, ASG가
            # 알아서 대체를 띄운다. 우리가 할 일은 진행 중 작업을 살려 보내는 것뿐이다.
            time.sleep(POLL_SEC)
            continue

        states = read_states()
        # OPTIONAL 은 "바쁘다"로만 센다. 없거나 멎은 건 보류 사유가 아니다
        # (위 OPTIONAL 주석 참고 — 보류로 치면 빈 인스턴스가 영원히 돈다).
        missing = [q for q, v in states.items() if v is None and q not in OPTIONAL]
        stale   = [q for q, v in states.items()
                   if v and v[2] > STALE_SEC and q not in OPTIONAL]
        # OPTIONAL 은 **신선한** busy 만 센다. 백엔드가 busy=True 를 쓴 직후
        # 죽으면 그 파일이 영영 남는다 — 나이를 안 보면 그걸로 보류가 굳는다.
        busy    = [q for q, v in states.items()
                   if v and v[0] and (q not in OPTIONAL or v[2] <= STALE_SEC)]

        if missing or stale or busy:
            if idle_since is not None:
                print(f"[reaper] 유휴 해제 (미기동={missing} 응답없음={stale} 작업중={busy})")
            idle_since = None
            time.sleep(POLL_SEC)
            continue

        if idle_since is None:
            idle_since = time.time()
            print("[reaper] 전원 유휴 진입 — 카운트 시작")

        idle_for = time.time() - idle_since
        if idle_for < IDLE_EXIT_SEC:
            time.sleep(POLL_SEC)
            continue

        if not queues_empty():
            idle_since = None
            time.sleep(POLL_SEC)
            continue

        print(f"[reaper] {int(idle_for)}초 유휴 + 큐 비었음 — 인스턴스 회수")
        if DRY_RUN or not iid:
            # DRAIN도 만들지 않는다. 만들면 로컬 워커가 진짜로 종료해버려서
            # "판단 로직만 본다"는 목적이 깨진다.
            print("[reaper] DRY_RUN — 실제로는 종료하지 않는다")
            idle_since = None
            time.sleep(POLL_SEC)
            continue
        # 종료 요청과 실제 셧다운 사이의 몇 초 동안 워커가 새 작업을 집지 않게 한다.
        set_drain("idle")
        try:
            terminate_self(iid)
        except Exception as e:
            print(f"[reaper] 종료 요청 실패: {e}")
            idle_since = None
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
