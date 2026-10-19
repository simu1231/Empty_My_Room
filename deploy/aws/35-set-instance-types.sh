#!/bin/bash
# 이미 만들어진 두 ASG(스팟/온디맨드)의 후보 인스턴스 타입을 config.sh 의
# INSTANCE_TYPES 로 맞춘다. 기본은 **계획만 출력**하고, EMR_APPLY=1 일 때만 바꾼다.
#
# 왜 30-asg.sh 로 안 되나: 거기서는 create-auto-scaling-group 이 "이미 있음"으로
# 건너뛰므로 기존 그룹의 타입은 안 바뀐다. 타입을 바꾸는 길은 이 스크립트뿐이다.
# 시작 템플릿에는 타입이 없어서(20-launch-template.sh 머리말) 새 버전도 필요 없다.
#
#   bash 35-set-instance-types.sh                       # 계획만(AWS 는 읽기만 한다)
#   EMR_GPU_PROFILE=trial-g6xl EMR_TRIAL_ACK=1 EMR_APPLY=1 bash 35-set-instance-types.sh
#   EMR_APPLY=1 bash 35-set-instance-types.sh           # 롤백: stable(g6.2xlarge g5.2xlarge)
#
# 안전장치
#   - 두 그룹 모두 desired=0 이고 인스턴스가 0대일 때만 바꾼다. 타입 변경은 이미 떠 있는
#     인스턴스를 교체하지 않으므로, 떠 있는 채로 바꾸면 "무엇이 도는지" 알 수 없게 된다.
#   - stable 이 아닌 프로파일은 EMR_TRIAL_ACK=1 이 있어야 한다. 측정 전 후보를 실수로
#     운영에 적용하지 않게 하려는 장치다.
#   - 서브넷은 현재 그룹 것을 쓰되, 새 타입을 안 파는 AZ 는 뺀다(30-asg.sh 와 같은 이유).
#   - **시험(trial) 프로파일은 ASG 가 실제로 쓸 시작 템플릿 버전을 먼저 검증한다.** 두 가지를 모두 본다:
#       ① userdata: fal 모드로 렌더됐고, 이번에 추가한 안전장치(기능 표식 + SSM 읽기 + 게이트 끔 + 옛 AMI 차단 가드)가
#          전부 들어 있다(표식만 있거나 fal 설정만 있으면 통과시키지 않는다).
#       ② AMI: FalReady 태그가 이 저장소 HEAD 의 fal 파일 해시와 일치한다(fal-ready.sh — AMI ID 가 새것이라는
#          사실로는 판단하지 않는다).
#     조회 실패·누락·불일치는 전부 ASG 변경 전에 중단한다. 계획 출력(dry-run)에서도 같다.
#     이유: xlarge 는 RAM 이 모자라 로컬 SAM3D 를 못 돈다. 시작 템플릿이 옛 AMI 와 fal userdata 의 조합(v19 가
#     그랬다)이면 워커가 조용히 로컬로 돌아 스래싱한다.
#   - stable(롤백)은 검증하지 않고 막지도 않는다 — 탈출구여야 한다(LT/AMI 조회가 실패해도 계속한다). 대신 어떤 LT
#     버전/AMI 가 적용되는지 찍고, fal userdata + 미검증 AMI 조합이면 경고한다.
set -euo pipefail
cd "$(dirname "$0")" && source ./config.sh && source ./fal-ready.sh

die() { echo "✗ $*" >&2; exit 1; }
# 조회가 실패하면 빈 값으로 흘리지 않고 중단한다(set -e 가 $(...) 안에서는 메시지 없이 죽는다).
aq() { local o; o=$("$@" 2>&1) || die "AWS 조회 실패: ${*:1:6} … — ${o:0:160}"; printf '%s' "$o"; }

APPLY=${EMR_APPLY:-0}

if [ "$EMR_GPU_PROFILE" != stable ] && [ "${EMR_TRIAL_ACK:-0}" != 1 ]; then
  echo "✗ 프로파일 $EMR_GPU_PROFILE 은 측정 전 후보다 — 운영 그룹에 적용하려면 EMR_TRIAL_ACK=1 을 명시하라." >&2
  echo "  (deploy/aws/TRIAL_G6XL.md 의 측정 통과 기준을 먼저 볼 것)" >&2
  exit 1
fi

echo "▶ 프로파일 $EMR_GPU_PROFILE → 타입 [$INSTANCE_TYPES] (SAM3D_BACKEND=$EMR_SAM3D_BACKEND)"

grp() { aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$1" \
          --query "AutoScalingGroups[0].$2" --output text; }

for g in "$ASG_SPOT" "$ASG_OD"; do
  d=$(grp "$g" DesiredCapacity); n=$(grp "$g" 'length(Instances)')
  if [ "$d" = None ] || [ -z "$d" ]; then echo "✗ ASG $g 가 없다 — 30-asg.sh 를 먼저" >&2; exit 1; fi
  if [ "$d" != 0 ] || [ "$n" != 0 ]; then
    echo "✗ $g: desired=$d 인스턴스=$n — 0/0 일 때만 바꾼다(떠 있는 인스턴스는 교체되지 않는다)" >&2
    exit 1
  fi
  echo "  $g 현재: $(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$g" \
      --query 'AutoScalingGroups[0].MixedInstancesPolicy.LaunchTemplate.Overrides[].InstanceType' --output text | tr '\t' ' ')"
done

# ── 시작 템플릿 / AMI 검증 ─────────────────────────────────────────────────
# ASG 가 지정한 LT 버전($Default 등)을 그대로 풀어서, 그 버전의 AMI 와 userdata 를 본다.
describe_lt() {   # $1=ASG → 전역 LT_NAME_X LT_SPEC LT_VER LT_AMI LT_UD(복호화) 설정
  local spec o ud
  spec=$(aq aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$1" \
           --query 'AutoScalingGroups[0].MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.[LaunchTemplateName,Version]' --output text)
  read -r LT_NAME_X LT_SPEC <<<"$spec"
  [ -n "${LT_NAME_X:-}" ] && [ "$LT_NAME_X" != None ] && [ -n "${LT_SPEC:-}" ] && [ "$LT_SPEC" != None ] \
    || die "$1 의 시작 템플릿 지정을 읽지 못했다(spec='$spec')"
  o=$(aq aws ec2 describe-launch-template-versions --launch-template-name "$LT_NAME_X" --versions "$LT_SPEC" \
        --query 'LaunchTemplateVersions[0].[VersionNumber,LaunchTemplateData.ImageId,LaunchTemplateData.UserData]' --output text)
  read -r LT_VER LT_AMI ud <<<"$o"
  [ -n "${LT_VER:-}" ] && [ "$LT_VER" != None ] || die "LT $LT_NAME_X $LT_SPEC 버전을 못 찾았다"
  [ -n "${LT_AMI:-}" ] && [ "$LT_AMI" != None ] || die "LT $LT_NAME_X v$LT_VER 에 AMI ID 가 없다"
  [ -n "${ud:-}" ] && [ "$ud" != None ] || die "LT $LT_NAME_X v$LT_VER 에 userdata 가 없다"
  LT_UD=$(printf '%s' "$ud" | base64 -d 2>/dev/null) || die "LT v$LT_VER userdata 를 base64 로 풀지 못했다"
  [ -n "$LT_UD" ] || die "LT v$LT_VER userdata 가 비어 있다"
}

describe_ami() {  # $1=AMI → 전역 AMI_STATE AMI_GITSHA AMI_FAL
  local o
  o=$(aq aws ec2 describe-images --image-ids "$1" \
        --query "Images[0].[State,Tags[?Key=='GitSha'].Value|[0],Tags[?Key=='FalReady'].Value|[0]]" --output text)
  read -r AMI_STATE AMI_GITSHA AMI_FAL <<<"$o"
  [ -n "${AMI_STATE:-}" ] && [ "$AMI_STATE" != None ] || die "AMI $1 를 조회하지 못했다(o='$o')"
  [ "$AMI_GITSHA" = None ] && AMI_GITSHA="(없음)"
  [ "$AMI_FAL" = None ] && AMI_FAL=""
  return 0
}

WANT_HASH=$(fal_ready_hash) || WANT_HASH=""

inspect_group() {   # $1=ASG. 시험(trial)에서는 실패하면 die 로 중단, stable 에서는 서브셸이라 이 함수만 끝난다.
  local g=$1 mode why
  describe_lt "$g"
  describe_ami "$LT_AMI"
  mode=local; printf '%s' "$LT_UD" | grep -qF 'if [ "fal" = fal ]; then' && mode=fal
  echo "▶ $g 가 쓰는 시작 템플릿: $LT_NAME_X v$LT_VER (지정 $LT_SPEC) — AMI $LT_AMI(GitSha=$AMI_GITSHA, FalReady=${AMI_FAL:-없음}), userdata=$mode"

  if [ "$EMR_GPU_PROFILE" = stable ]; then
    # 롤백: 막지 않는다. 적용될 조합만 분명히 알린다.
    echo "  (stable 복귀는 시작 템플릿을 바꾸지 않는다 — 위 LT v$LT_VER / AMI $LT_AMI 가 그대로 적용된다)"
    if [ "$mode" = fal ] && { [ -z "$AMI_FAL" ] || [ "$AMI_FAL" != "$WANT_HASH" ]; }; then
      echo "  ! 경고: userdata 는 fal 인데 AMI 가 fal-ready 로 검증되지 않았다 — 워커가 조용히 local 로 돈다(g6.2xlarge 이상에서는 동작)"
    fi
    return 0
  fi

  # 시험 프로파일: 전부 통과해야 한다.
  [ -n "$WANT_HASH" ] || die "저장소 HEAD 에서 fal 해시를 계산하지 못했다 — git 저장소/커밋 상태를 확인하라"
  why=$(printf '%s' "$LT_UD" | fal_userdata_check) || {
    echo "$why" | sed 's/^/    /' >&2
    die "LT $LT_NAME_X v$LT_VER 의 userdata 가 fal 안전장치를 전부 담지 않았다 — 20-launch-template.sh 로 새 버전을 만들어라"; }
  [ "$AMI_STATE" = available ] || die "AMI $LT_AMI 상태가 $AMI_STATE 다(available 이어야 한다)"
  [ -n "$AMI_FAL" ] || die "AMI $LT_AMI 에 FalReady 태그가 없다 — fal 전환 코드 검증을 거치지 않은 AMI 다(옛 AMI 로는 워커가 조용히 local 로 돈다). 빌더에서 verify-ami.sh 가 출력하는 create-image 로 만든 AMI 가 필요하다"
  [ "$AMI_FAL" = "$WANT_HASH" ] || die "AMI $LT_AMI 의 FalReady=$AMI_FAL 가 저장소 HEAD($WANT_HASH)와 다르다 — 이 코드로 구워진 AMI 가 아니다(fal 파일이 바뀌었거나 아직 푸시 전)"
  echo "  ✔ userdata 안전장치 전부 확인, AMI FalReady=$AMI_FAL 가 HEAD 와 일치"
}

for g in "$ASG_SPOT" "$ASG_OD"; do
  if [ "$EMR_GPU_PROFILE" = stable ]; then
    # 롤백은 정보 조회가 실패해도 막지 않는다(조회 권한/네트워크 문제로 탈출구가 막히면 안 된다).
    ( inspect_group "$g" ) || echo "  ! $g 의 시작 템플릿/AMI 를 조회하지 못했다 — 롤백은 계속하지만 어떤 LT/AMI 가 적용되는지는 확인하지 못했다"
  else
    inspect_group "$g"
  fi
done

# 새 타입을 전부 파는 AZ 의 서브넷만 남긴다(현재 그룹의 서브넷이 후보다).
CUR_SUBNETS=$(grp "$ASG_SPOT" VPCZoneIdentifier)
keep=""
for sn in ${CUR_SUBNETS//,/ }; do
  az=$(aws ec2 describe-subnets --subnet-ids "$sn" --query 'Subnets[0].AvailabilityZone' --output text)
  ok=1
  for t in $INSTANCE_TYPES; do
    aws ec2 describe-instance-type-offerings --location-type availability-zone \
      --filters "Name=instance-type,Values=$t" "Name=location,Values=$az" \
      --query 'InstanceTypeOfferings[0].Location' --output text | grep -q "$az" || { ok=0; break; }
  done
  if [ "$ok" = 1 ]; then keep="$keep,$sn"; else echo "  제외 $sn ($az) — $t 를 팔지 않는다"; fi
done
NEW_SUBNETS=${keep#,}
[ -n "$NEW_SUBNETS" ] || { echo "✗ [$INSTANCE_TYPES] 를 전부 파는 AZ 가 현재 서브넷 중에 없다" >&2; exit 1; }
echo "  서브넷: $CUR_SUBNETS → $NEW_SUBNETS"

OVERRIDES=$(for t in $INSTANCE_TYPES; do printf '{"InstanceType":"%s"},' "$t"; done | sed 's/,$//')

# 30-asg.sh 와 같은 정책 JSON. 거기서 바꾸면 여기도 같이 바꿀 것.
cat > /tmp/emr-mip-spot.update.json <<JSON
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
cat > /tmp/emr-mip-od.update.json <<JSON
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

CMD_SPOT=(aws autoscaling update-auto-scaling-group --auto-scaling-group-name "$ASG_SPOT"
          --mixed-instances-policy file:///tmp/emr-mip-spot.update.json --vpc-zone-identifier "$NEW_SUBNETS")
CMD_OD=(aws autoscaling update-auto-scaling-group --auto-scaling-group-name "$ASG_OD"
        --mixed-instances-policy file:///tmp/emr-mip-od.update.json --vpc-zone-identifier "$NEW_SUBNETS")

if [ "$APPLY" != 1 ]; then
  echo
  echo "계획만 출력했다(아무것도 바꾸지 않았다). 적용하려면 EMR_APPLY=1 을 붙여 다시 실행."
  echo "  ${CMD_SPOT[*]}"
  echo "  ${CMD_OD[*]}"
  exit 0
fi

echo "▶ 적용"
"${CMD_SPOT[@]}"
"${CMD_OD[@]}"
echo "✔ 타입 변경 완료 — desired 는 0 그대로다(아직 아무것도 안 뜬다)."
echo "  되돌리기: EMR_APPLY=1 bash $0   (EMR_GPU_PROFILE 미설정 = stable = g6.2xlarge g5.2xlarge)"
