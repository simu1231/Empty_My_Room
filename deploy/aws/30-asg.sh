#!/bin/bash
# ASG 두 개를 만든다. 왜 둘인가 — 여기가 4단계에서 가장 설명이 필요한 부분이다.
#
# 사용자는 "스팟 우선 + 온디맨드 폴백"을 골랐다. 그런데 **ASG에는 그런 기능이 없다.**
# 혼합 인스턴스 정책의 OnDemandPercentage는 "정상일 때의 배합 비율"일 뿐,
# 스팟 용량이 없을 때 대신 온디맨드를 사는 동작이 아니다. 스팟이 안 잡히면 ASG는
# 그냥 조용히 재시도만 반복하고, 큐는 계속 쌓인다.
#
# 그래서 폴백을 직접 만든다. 가장 단순한 방법이 그룹을 둘로 두는 것이다.
#
#   emr-gpu-spot      : 평소 여기만 쓴다. 전량 스팟. min=0.
#   emr-gpu-ondemand  : 평소 0대. "큐에 일이 있는데 5분째 한 대도 못 떴다"는
#                       알람이 울릴 때만 1대 뜬다. 전량 온디맨드.
#
# Lambda도 EventBridge도 필요 없다. CloudWatch 알람 하나와 ASG 정책 하나면 된다.
# 회복도 저절로 된다 — 스팟이 다시 잡히면 온디맨드 쪽은 할 일이 없어 유휴가 되고,
# 리퍼가 알아서 내린다.
set -euo pipefail
cd "$(dirname "$0")" && source ./config.sh

: "${EMR_SUBNETS:?서브넷을 지정하세요(쉼표 구분): EMR_SUBNETS=subnet-a,subnet-b,subnet-c $0}"

# 후보 인스턴스 타입을 Overrides JSON으로 만든다.
OVERRIDES=$(for t in $INSTANCE_TYPES; do printf '{"InstanceType":"%s"},' "$t"; done | sed 's/,$//')

common_args=(
  --vpc-zone-identifier "$EMR_SUBNETS"
  --min-size 0
  --desired-capacity 0
  # EC2 헬스체크는 "인스턴스가 켜져 있는지"만 본다. 앱이 죽어도 정상으로 본다.
  # 그래서 이것만 믿으면 컴포즈가 안 뜬 빈 GPU 인스턴스가 영원히 과금된다
  # (g6.xlarge 월 $327). 애플리케이션 수준 판단은 ASG가 아니라 인스턴스 안의
  # 두 장치가 한다:
  #   리퍼   — 일이 없으면 스스로 물러난다(정상 경로)
  #   가디언 — 리퍼조차 없으면 물러나게 한다(호스트 systemd, 컴포즈 바깥)
  # ELB 헬스체크를 쓰지 않는 이유는 이 ASG에 로드밸런서가 붙지 않기 때문이다.
  #
  # 헬스체크 유예. 우리 인스턴스는 부팅 + 모델 로드에 3분쯤 걸린다.
  # 이보다 짧으면 아직 준비 중인 인스턴스를 "불건전"으로 보고 죽였다 다시 띄운다.
  --health-check-type EC2
  --health-check-grace-period 300
  # 기본 워밍업. 스케일 정책이 따로 안 정하면 이 값을 쓴다.
  --default-instance-warmup "$WARMUP_SEC"
  # 스팟 회수 예고(rebalance recommendation)를 받으면 회수당하기 전에 미리
  # 대체 인스턴스를 띄운다. 2분 통지를 기다리는 것보다 빈 시간이 짧아진다.
  --capacity-rebalance
  # Project 태그는 장식이 아니다. 10-iam.sh의 종료 권한 Condition이
  # autoscaling:ResourceTag/Project 를 보므로, 이 태그가 없으면 리퍼가
  # AccessDenied로 인스턴스를 못 내린다 = 스케일투제로가 통째로 안 된다.
  --tags "Key=Project,Value=emr,PropagateAtLaunch=true"
)

# ── 1. 스팟 그룹 ────────────────────────────────────────────────────────
echo "▶ $ASG_SPOT (전량 스팟, 최대 ${MAX_SPOT}대)"
cat > /tmp/emr-mip-spot.json <<JSON
{
  "LaunchTemplate": {
    "LaunchTemplateSpecification": {"LaunchTemplateName": "${LT_NAME}", "Version": "\$Default"},
    "Overrides": [${OVERRIDES}]
  },
  "InstancesDistribution": {
    "OnDemandBaseCapacity": 0,
    "OnDemandPercentageAboveBaseCapacity": 0,
    "SpotAllocationStrategy": "price-capacity-optimized"
  }
}
JSON
# price-capacity-optimized 를 쓰는 이유: lowest-price는 가장 싼 풀만 골라서
# 그 풀이 고갈되면 바로 회수당한다. capacity-optimized는 회수는 적지만 비쌀 수
# 있다. 둘을 같이 보는 price-capacity-optimized가 배치형 워크로드에 맞다.

aws autoscaling create-auto-scaling-group \
  --auto-scaling-group-name "$ASG_SPOT" \
  --max-size "$MAX_SPOT" \
  --mixed-instances-policy file:///tmp/emr-mip-spot.json \
  "${common_args[@]}" 2>/dev/null || echo "  (이미 있음 — 건너뜀)"

# ── 2. 온디맨드 폴백 그룹 ───────────────────────────────────────────────
echo "▶ $ASG_OD (전량 온디맨드, 최대 ${MAX_OD}대, 평소 0대)"
cat > /tmp/emr-mip-od.json <<JSON
{
  "LaunchTemplate": {
    "LaunchTemplateSpecification": {"LaunchTemplateName": "${LT_NAME}", "Version": "\$Default"},
    "Overrides": [${OVERRIDES}]
  },
  "InstancesDistribution": {
    "OnDemandBaseCapacity": 0,
    "OnDemandPercentageAboveBaseCapacity": 100,
    "OnDemandAllocationStrategy": "lowest-price"
  }
}
JSON

aws autoscaling create-auto-scaling-group \
  --auto-scaling-group-name "$ASG_OD" \
  --max-size "$MAX_OD" \
  --mixed-instances-policy file:///tmp/emr-mip-od.json \
  "${common_args[@]}" 2>/dev/null || echo "  (이미 있음 — 건너뜀)"

echo "✔ ASG 두 개 준비 완료 (둘 다 desired=0 — 아직 아무것도 안 뜬다)"
