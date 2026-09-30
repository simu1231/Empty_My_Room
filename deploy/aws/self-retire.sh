#!/bin/bash
# 이 인스턴스를 ASG에서 스스로 빼고 종료시킨다. 요금을 끊는 유일한 경로다.
#
# 왜 별도 파일인가 — 이 로직이 필요한 곳이 둘이다(가디언, userdata 실패 트랩).
# 각자 구현하면 한쪽만 고치는 실수가 난다. 특히 --should-decrement-desired-capacity
# 를 빼먹는 실수는 증상이 "스케일투제로가 안 된다"가 아니라 "인스턴스가 무한히
# 재생성된다"로 나타나서 원인을 찾기 어렵다.
#
# shutdown / halt 는 대안이 아니다. ASG가 unhealthy로 보고 desired를 유지한 채
# 교체 인스턴스를 띄운다 — 요금이 그대로 이어진다. 반드시 ASG API로 빼야 한다.
#
# 사용법: self-retire.sh "<이유>"
set -uo pipefail
REASON=${1:-"이유 없음"}
DRY_RUN=${EMR_RETIRE_DRY_RUN:-0}
log() { echo "[self-retire] $*"; }

log "회수 요청: $REASON"

if ! command -v aws >/dev/null 2>&1; then
  # 여기 오면 끊을 방법이 없다. 그래서 verify-ami.sh가 굽기 전에 막는다.
  log "치명적: aws CLI가 없어서 회수할 수 없다. AMI가 잘못 구워졌다."
  exit 1
fi

# IMDSv2 필수(시작 템플릿에서 HttpTokens=required). --max-time 이 없으면
# EC2가 아닌 곳에서 이 링크로컬 주소가 블랙홀이라 타이머가 멈춘 채 쌓인다.
TOKEN=$(curl -sf --max-time 3 -X PUT "http://169.254.169.254/latest/api/token" \
          -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || echo "")
[ -z "$TOKEN" ] && { log "IMDS 토큰 실패 — EC2가 아닌가?"; exit 1; }
IID=$(curl -sf --max-time 3 -H "X-aws-ec2-metadata-token: $TOKEN" \
        "http://169.254.169.254/latest/meta-data/instance-id" || echo "")
REGION=$(curl -sf --max-time 3 -H "X-aws-ec2-metadata-token: $TOKEN" \
        "http://169.254.169.254/latest/meta-data/placement/region" || echo "")
[ -z "$IID" ] && { log "instance-id를 못 읽었다"; exit 1; }
export AWS_DEFAULT_REGION=${REGION:-ap-northeast-2}

# ASG 소속이 아니면 손대지 않는다. AMI를 굽는 중이거나 수동으로 띄운 디버그
# 인스턴스를 죽이면 곤란하다.
ASG=$(aws autoscaling describe-auto-scaling-instances --instance-ids "$IID" \
        --query 'AutoScalingInstances[0].AutoScalingGroupName' --output text 2>/dev/null || echo "None")
if [ -z "$ASG" ] || [ "$ASG" = "None" ]; then
  log "$IID 는 ASG 소속이 아니다 — 종료하지 않는다"
  exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN — 실제로는 종료하지 않는다 (ASG=$ASG instance=$IID)"
  exit 0
fi

log "ASG $ASG 에서 $IID 종료 요청(desired -1)"
aws autoscaling terminate-instance-in-auto-scaling-group \
  --instance-id "$IID" --should-decrement-desired-capacity >/dev/null \
  && { log "종료 요청 완료"; exit 0; } || { log "종료 요청 실패"; exit 1; }
