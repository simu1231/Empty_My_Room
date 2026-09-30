"""
0대 → 1대 기동을 API 서버가 직접 요청한다.

■ 왜 CloudWatch 알람에 맡기지 않는가
AWS 문서에 이렇게 적혀 있다. "큐가 6시간 넘게 비활성이면 SQS는 지표 전송을
멈춘다", 그리고 "비활성 상태에서 다시 활성화될 때 CloudWatch 지표에 **최대
15분의 지연**이 발생한다."

우리 서비스는 하루 수십 세션이라 밤새 큐가 잔다. 아침 첫 요청이 들어오면
지표가 다시 흐르기까지 최대 15분, 거기에 알람 평가 1분과 부팅 3분이 붙는다.
사용자는 메쉬 하나를 19분 기다린다. 알람은 이 경로에 쓸 수 없다.

게다가 타깃 추적 정책은 0대에서 애초에 동작하지 않는다. 지표가
"인스턴스당 백로그"라 분모가 0이면 값이 정의되지 않기 때문이다.

■ 그래서 누가 하는가
접수 순간을 정확히 아는 쪽, 즉 이 API 서버다. 상시 가동이고 SQS에 메시지를
넣는 바로 그 코드가 여기 있다. desired=0이면 1로 올린다. 지연 0초다.

CloudWatch 알람은 버리지 않고 역할만 바꾼다.
  - 1대 → N대 증설: 알람이 한다. 그때는 큐가 이미 활성이라 지표가 1분마다 온다.
  - 스팟이 안 뜰 때 온디맨드 폴백: 알람이 한다(SQS가 아니라 ASG 지표를 본다).
  - N대 → 0대 축소: 리퍼가 한다.
"""
import os
import threading
import time

import boto3
from botocore.config import Config

ASG_NAME = os.getenv("ASG_SPOT", "")          # 비면 아무것도 안 한다(로컬 기본)
REGION   = os.getenv("AWS_REGION", "ap-northeast-2")

# 같은 세션에서 접수가 연달아 7건 들어오면(사진 1장 = sam3d 3 + scene 4),
# 매번 AWS를 부를 이유가 없다. 이 간격 안에서는 한 번만 부른다.
NUDGE_INTERVAL_SEC = int(os.getenv("CAPACITY_NUDGE_INTERVAL", "30"))

_lock = threading.Lock()
_last_nudge = 0.0
_asg = None


def _client():
    global _asg
    if _asg is None:
        _asg = boto3.client("autoscaling",
                            config=Config(region_name=REGION,
                                          retries={"max_attempts": 2, "mode": "standard"}))
    return _asg


def _nudge():
    try:
        g = _client().describe_auto_scaling_groups(
            AutoScalingGroupNames=[ASG_NAME])["AutoScalingGroups"]
        if not g:
            print(f"[capacity] ASG {ASG_NAME}를 찾을 수 없습니다")
            return
        g = g[0]
        desired, maxs = g["DesiredCapacity"], g["MaxSize"]
        alive = [i for i in g["Instances"]
                 if i["LifecycleState"] not in ("Terminating", "Terminating:Wait",
                                                "Terminating:Proceed", "Terminated")]
        if desired > 0 and alive:
            return          # 이미 떠 있거나 뜨는 중 — 증설은 알람에 맡긴다
        if desired >= maxs:
            return
        print(f"[capacity] {ASG_NAME} desired {desired} → 1 (첫 작업 접수)")
        # HonorCooldown=False: 방금 축소한 직후라도 즉시 올린다. 쿨다운을 지키면
        # 리퍼가 인스턴스를 내린 직후 들어온 요청이 쿨다운만큼 그냥 기다린다.
        _client().set_desired_capacity(
            AutoScalingGroupName=ASG_NAME, DesiredCapacity=1, HonorCooldown=False)
    except Exception as e:
        # 여기서 실패해도 접수는 성공이다. 메시지는 큐에 남아 있고, 백로그 알람이
        # 늦게라도 인스턴스를 띄운다. 그래서 알람을 안전망으로 남겨둔 것이다.
        print(f"[capacity] 용량 요청 실패(무시): {type(e).__name__}: {e}")


def request_capacity():
    """
    접수 직후에 부른다. 논블로킹 — 사용자 응답을 AWS API 왕복만큼 늦추지 않는다.
    """
    global _last_nudge
    if not ASG_NAME:
        return
    with _lock:
        if time.time() - _last_nudge < NUDGE_INTERVAL_SEC:
            return
        _last_nudge = time.time()
    threading.Thread(target=_nudge, daemon=True).start()
