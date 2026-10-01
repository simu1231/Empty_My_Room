#!/bin/bash
# 배포 전 사전점검. 나머지 스크립트보다 먼저 돌린다.
#
# 왜 필요한가? 신규 AWS 계정은 G 계열(GPU) 인스턴스 쿼터가 **0**이다. 쿼터가 0인
# 채로 10~40번 스크립트를 다 돌리면 전부 "성공"한다 — IAM 역할도, 시작 템플릿도,
# ASG도, 알람도 다 만들어진다. 그런데 정작 인스턴스가 한 대도 안 뜬다.
# ASG는 용량 부족을 조용히 재시도만 하고, 폴백 알람도 5분마다 온디맨드를
# 시도했다가 같은 이유로 실패한다. 어디가 틀렸는지 로그에 잘 안 드러난다.
#
# 쿼터 증설은 자동 승인이 아니라 사람 심사(몇 시간~며칠)라서, 배포 당일에
# 발견하면 그날은 못 한다. 그래서 맨 앞에서 막는다.
set -euo pipefail
cd "$(dirname "$0")" && source ./config.sh

FAIL=0

# ── 1. CLI와 자격증명 ────────────────────────────────────────────────
echo "▶ CLI / 자격증명"
command -v aws >/dev/null || { echo "  ✗ aws CLI가 PATH에 없다"; exit 1; }
echo "  aws $(aws --version 2>&1 | cut -d' ' -f1 | cut -d/ -f2)"
if ! IDENT=$(aws sts get-caller-identity --output text --query '[Account,Arn]' 2>&1); then
  echo "  ✗ 자격증명 실패: $IDENT"; exit 1
fi
echo "  계정 $(echo "$IDENT" | cut -f1) / $(echo "$IDENT" | cut -f2)"
echo "  리전 $AWS_REGION"

# ── 2. 인스턴스 타입별 vCPU ──────────────────────────────────────────
# 쿼터 단위가 대수가 아니라 vCPU다. INSTANCE_TYPES 중 가장 큰 타입이 선택될 수
# 있으므로(스팟 용량 확보용으로 여러 타입을 섞어놨다) 최악의 경우로 계산한다.
echo "▶ 인스턴스 타입 vCPU"
MAX_VCPU=0
for t in $INSTANCE_TYPES; do
  v=$(aws ec2 describe-instance-types --instance-types "$t" \
        --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text 2>/dev/null || echo "")
  if [ -z "$v" ] || [ "$v" = "None" ]; then
    echo "  ! $t 은 이 리전에서 조회 안 됨 — 건너뜀"
    continue
  fi
  echo "  $t = ${v} vCPU"
  [ "$v" -gt "$MAX_VCPU" ] && MAX_VCPU=$v
done
[ "$MAX_VCPU" -eq 0 ] && { echo "  ✗ vCPU를 하나도 못 구했다"; exit 1; }

NEED_SPOT=$(( MAX_VCPU * MAX_SPOT ))
NEED_OD=$(( MAX_VCPU * MAX_OD ))
echo "  최악의 경우 필요량: 스팟 ${NEED_SPOT} vCPU (${MAX_VCPU}×${MAX_SPOT}), 온디맨드 ${NEED_OD} vCPU (${MAX_VCPU}×${MAX_OD})"

# ── 3. 쿼터 확인 ─────────────────────────────────────────────────────
# L-3819A6DF  All G and VT Spot Instance Requests
# L-DB2E81BA  Running On-Demand G and VT instances
echo "▶ GPU 쿼터"
check_quota() {   # $1=코드 $2=필요량 $3=사람이 읽을 이름
  local code=$1 need=$2 label=$3 cur
  cur=$(aws service-quotas get-service-quota --service-code ec2 --quota-code "$code" \
          --query 'Quota.Value' --output text 2>/dev/null || echo "")
  if [ -z "$cur" ]; then echo "  ✗ $label 쿼터 조회 실패 (ServiceQuotasReadOnlyAccess 권한 확인)"; FAIL=1; return; fi
  cur=${cur%.*}
  if [ "$cur" -ge "$need" ]; then
    echo "  ✓ $label: ${cur} vCPU (필요 ${need})"
    # 여유가 0이면 "지금 구성은 되지만 한 대도 더 못 뜬다"는 뜻이다.
    # 후보 타입을 넓히거나 MAX_* 를 올리려면 쿼터 증설이 먼저다.
    [ "$cur" -eq "$need" ] && echo "    └ 여유 0 — 구성을 키우려면 쿼터부터 올려야 한다"
  else
    echo "  ✗ $label: ${cur} vCPU — ${need} 필요"
    FAIL=1
    # 대기 중인 신청이 있으면 알려준다. 신청조차 안 했으면 그 사실이 더 중요하다.
    local pend
    pend=$(aws service-quotas list-requested-service-quota-change-history-by-quota \
             --service-code ec2 --quota-code "$code" \
             --query "RequestedQuotas[?Status=='PENDING'||Status=='CASE_OPENED'].[DesiredValue,Status]" \
             --output text 2>/dev/null || echo "")
    if [ -n "$pend" ]; then
      echo "    └ 신청 대기 중: $(echo "$pend" | tr '\n' ' ') — 승인까지 몇 시간~며칠"
    else
      echo "    └ 신청 이력 없음. Service Quotas 콘솔에서 리전을 $AWS_REGION 으로 맞추고 신청할 것"
      echo "      (리전 안 바꾸면 버지니아 쿼터가 올라가서 아무 효과가 없다)"
    fi
  fi
}
check_quota L-3819A6DF "$NEED_SPOT" "스팟 G/VT"
check_quota L-DB2E81BA "$NEED_OD"   "온디맨드 G/VT"

# ── 4. 결과 ──────────────────────────────────────────────────────────
echo
if [ "$FAIL" -ne 0 ]; then
  echo "✗ 사전점검 실패 — 10-iam.sh 이후를 돌리지 말 것."
  echo "  지금 배포해도 리소스는 다 만들어지고 인스턴스만 안 뜬다(원인 추적이 어렵다)."
  exit 1
fi
echo "✓ 사전점검 통과 — 10-iam.sh 부터 순서대로 실행"
