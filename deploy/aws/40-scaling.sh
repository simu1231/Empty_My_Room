#!/bin/bash
# 스케일 정책과 알람.
#
# ■ 세 가지 전이를 서로 다른 장치가 담당한다. 한 장치로 다 하려다 실패하는 게
#   스케일투제로에서 가장 흔한 실수다.
#
#   0대 → 1대   API 서버가 접수 순간에 직접 SetDesiredCapacity(1).  (api/capacity.py)
#               알람을 안 쓰는 이유: 잠든 큐의 지표는 최대 15분 늦고,
#               타깃 추적은 분모가 0이라 애초에 0대에서 동작하지 않는다.
#   1대 → N대   여기서 만드는 백로그 알람 + 단계 조정 정책.
#               이 시점엔 큐가 활성이라 지표가 1분마다 정상적으로 온다.
#   N대 → 0대   리퍼가 인스턴스 안에서 판단.  (worker/idle_reaper.py)
#               CloudWatch는 "처리 중" 메시지를 큐에서 못 보므로,
#               가장 바쁜 순간이 가장 한가해 보인다. 축소를 맡기면 안 된다.
#
#   + 스팟이 안 뜰 때만 온디맨드 1대. ASG에 자동 폴백이 없어서 직접 만든다.
set -euo pipefail
cd "$(dirname "$0")" && source ./config.sh

echo "▶ ASG 그룹 지표 수집 켜기"
# 이걸 안 켜면 GroupInServiceInstances / GroupDesiredCapacity 가 아예 안 나온다.
# 폴백 알람이 그 둘을 보므로, 빠뜨리면 알람이 INSUFFICIENT_DATA로 굳고
# "스팟이 안 뜨는데 아무 일도 안 일어나는" 상태가 된다.
for g in "$ASG_SPOT" "$ASG_OD"; do
  aws autoscaling enable-metrics-collection \
    --auto-scaling-group-name "$g" --granularity "1Minute"
done

# ── 1. 증설 정책 (스팟 그룹) ────────────────────────────────────────────
echo "▶ 증설 단계 조정 정책"
# 왜 타깃 추적이 아니라 단계 조정(step scaling)인가:
#   타깃 추적은 "인스턴스당 백로그"를 지표로 쓰는데 그 값은 0대에서 정의되지
#   않는다. 게다가 CloudWatch 알람을 AWS가 직접 만들고 관리해서 우리가 손댈 수
#   없다. 단계 조정은 알람을 우리가 만들므로 결측 데이터 처리(notBreaching)를
#   지정할 수 있고, 이게 "밤에 큐가 자서 지표가 없는" 우리 상황에 꼭 필요하다.
SCALE_OUT_ARN=$(aws autoscaling put-scaling-policy \
  --auto-scaling-group-name "$ASG_SPOT" \
  --policy-name "${ASG_SPOT}-scale-out" \
  --policy-type StepScaling \
  --adjustment-type ChangeInCapacity \
  --estimated-instance-warmup "$WARMUP_SEC" \
  --step-adjustments \
     "MetricIntervalLowerBound=0,MetricIntervalUpperBound=${BACKLOG_STEP1},ScalingAdjustment=1" \
     "MetricIntervalLowerBound=${BACKLOG_STEP1},ScalingAdjustment=2" \
  --query PolicyARN --output text)
# 단계 경계는 알람 임계값(0)으로부터의 '거리'다. 백로그 1~10이면 +1대,
# 11 이상이면 +2대. MaxSize가 ${MAX_SPOT}이라 그 이상은 안 올라간다.
# estimated-instance-warmup 을 ${WARMUP_SEC}초로 둔 건, 방금 띄운 인스턴스가
# 모델을 올리는 동안은 "아직 일 못 하는 중"으로 쳐서 중복 증설을 막기 위함이다.

echo "▶ 백로그 알람 (두 큐의 대기 + 처리중 합)"
# 처리중(NotVisible)까지 더하는 게 핵심이다. 대기 메시지만 보면, 워커가 30초짜리
# 작업을 붙잡고 있는 동안 백로그가 0으로 보인다. 그 값으로는 "지금 일이 있다"를
# 판단할 수 없다.
aws cloudwatch put-metric-alarm \
  --alarm-name "${ASG_SPOT}-backlog" \
  --alarm-description "두 큐의 미완료 작업 수가 0보다 크면 증설" \
  --evaluation-periods 1 --datapoints-to-alarm 1 \
  --threshold 0 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "$SCALE_OUT_ARN" \
  --metrics '[
    {"Id":"s1","MetricStat":{"Metric":{"Namespace":"AWS/SQS","MetricName":"ApproximateNumberOfMessagesVisible","Dimensions":[{"Name":"QueueName","Value":"'"$Q_SAM3D"'"}]},"Period":60,"Stat":"Maximum"},"ReturnData":false},
    {"Id":"s2","MetricStat":{"Metric":{"Namespace":"AWS/SQS","MetricName":"ApproximateNumberOfMessagesNotVisible","Dimensions":[{"Name":"QueueName","Value":"'"$Q_SAM3D"'"}]},"Period":60,"Stat":"Maximum"},"ReturnData":false},
    {"Id":"c1","MetricStat":{"Metric":{"Namespace":"AWS/SQS","MetricName":"ApproximateNumberOfMessagesVisible","Dimensions":[{"Name":"QueueName","Value":"'"$Q_SCENE"'"}]},"Period":60,"Stat":"Maximum"},"ReturnData":false},
    {"Id":"c2","MetricStat":{"Metric":{"Namespace":"AWS/SQS","MetricName":"ApproximateNumberOfMessagesNotVisible","Dimensions":[{"Name":"QueueName","Value":"'"$Q_SCENE"'"}]},"Period":60,"Stat":"Maximum"},"ReturnData":false},
    {"Id":"backlog","Expression":"FILL(s1,0)+FILL(s2,0)+FILL(c1,0)+FILL(c2,0)","Label":"미완료 작업 수","ReturnData":true}
  ]'
# FILL(...,0) 이 필요한 이유: SQS는 해당 상태의 메시지가 하나도 없으면 그 지표를
# 아예 안 보낸다. 결측이 하나라도 섞이면 수식 전체가 결측이 되어 알람이
# INSUFFICIENT_DATA로 빠진다. 0으로 메워야 "sam3d에만 일이 있는" 정상 상황을
# 제대로 읽는다.

# ── 1-b. DLQ 알람 ───────────────────────────────────────────────────────
# 백로그 알람과 목적이 정반대다. 저건 "일이 있으니 켜라"이고 이건 "사람이 봐야
# 한다"이다. DLQ 에 왔다는 건 재시도를 다 쓰고도 안 됐다는 뜻이라, 자동으로
# 할 수 있는 일이 더 없다.
#
# 여기에 안 잡히는 실패가 있다: non-retryable 로 분류돼 큐에서 즉시 삭제된 건은
# DLQ 를 거치지 않는다. 그건 DynamoDB 의 failure_kind 로만 보인다
# (deploy/worker/worker.py 의 except 분기, deploy/worker/jobspec.py 참고).
#
# create-topic 은 멱등이라 ARN 조회를 겸한다. 토픽의 주인은 15-resources.sh 이고
# 이메일 구독도 거기서 한다 — 여기서는 보낼 곳만 알면 된다.
echo "▶ DLQ 알람"
TOPIC_ARN=$(aws sns create-topic --name "$EMR_ALERT_TOPIC" --query TopicArn --output text)
for Q in "$Q_SAM3D" "$Q_SCENE"; do
  aws cloudwatch put-metric-alarm \
    --alarm-name "${Q}-dlq-not-empty" \
    --alarm-description "${Q}-dlq 에 실패 작업이 쌓였다 — 사람이 봐야 한다" \
    --namespace AWS/SQS --metric-name ApproximateNumberOfMessagesVisible \
    --dimensions "Name=QueueName,Value=${Q}-dlq" \
    --statistic Maximum --period 300 \
    --evaluation-periods 1 --datapoints-to-alarm 1 \
    --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "$TOPIC_ARN"
  echo "  ${Q}-dlq ≥ 1 → $EMR_ALERT_EMAIL"
done

# ── 2. 온디맨드 폴백 ────────────────────────────────────────────────────
echo "▶ 온디맨드 폴백 정책"
OD_OUT_ARN=$(aws autoscaling put-scaling-policy \
  --auto-scaling-group-name "$ASG_OD" \
  --policy-name "${ASG_OD}-scale-out" \
  --policy-type StepScaling \
  --adjustment-type ChangeInCapacity \
  --estimated-instance-warmup "$WARMUP_SEC" \
  --step-adjustments "MetricIntervalLowerBound=0,ScalingAdjustment=1" \
  --query PolicyARN --output text)

echo "▶ 폴백 알람 (스팟 그룹이 ${OD_FALLBACK_MIN}분째 요청한 만큼 못 띄움)"
# 이 알람은 SQS를 아예 안 본다. ASG 자기 지표만 본다:
#   "원한 대수(desired) > 0 인데 실제 가동(InService)이 그보다 적다"가
#   ${OD_FALLBACK_MIN}분 연속이면 스팟 용량이 없는 것이다.
# SQS를 안 보니 "잠든 큐 15분 지연" 문제에서도 자유롭다. ASG 지표는
# 인스턴스가 0대여도 1분마다 계속 나온다.
aws cloudwatch put-metric-alarm \
  --alarm-name "${ASG_OD}-spot-unavailable" \
  --alarm-description "스팟이 ${OD_FALLBACK_MIN}분째 용량을 못 채우면 온디맨드 1대" \
  --evaluation-periods "$OD_FALLBACK_MIN" --datapoints-to-alarm "$OD_FALLBACK_MIN" \
  --threshold 0 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "$OD_OUT_ARN" \
  --metrics '[
    {"Id":"want","MetricStat":{"Metric":{"Namespace":"AWS/AutoScaling","MetricName":"GroupDesiredCapacity","Dimensions":[{"Name":"AutoScalingGroupName","Value":"'"$ASG_SPOT"'"}]},"Period":60,"Stat":"Average"},"ReturnData":false},
    {"Id":"have","MetricStat":{"Metric":{"Namespace":"AWS/AutoScaling","MetricName":"GroupInServiceInstances","Dimensions":[{"Name":"AutoScalingGroupName","Value":"'"$ASG_SPOT"'"}]},"Period":60,"Stat":"Average"},"ReturnData":false},
    {"Id":"gap","Expression":"IF(want > 0 AND FILL(have,0) < want, 1, 0)","Label":"스팟 미충족","ReturnData":true}
  ]'

# 온디맨드 그룹을 줄이는 알람은 만들지 않는다. 스팟이 회복되면 온디맨드 쪽
# 워커는 할 일이 없어 유휴가 되고, 그 인스턴스의 리퍼가 스스로 내린다.
# 축소 경로를 하나로 몰아두면 "둘이 동시에 줄이다 작업 중인 인스턴스를 죽이는"
# 경우가 생기지 않는다.

echo "✔ 스케일 정책 준비 완료"
echo
echo "  0→1  : API 서버 (api/capacity.py)  — 즉시"
echo "  1→N  : ${ASG_SPOT}-backlog 알람     — 약 1~2분"
echo "  폴백 : ${ASG_OD}-spot-unavailable 알람 — ${OD_FALLBACK_MIN}분"
echo "  N→0  : 리퍼 (worker/idle_reaper.py) — 유휴 ${IDLE_EXIT_SEC:-120}초"
