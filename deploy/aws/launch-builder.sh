#!/bin/bash
# AMI를 구울 **빌더 인스턴스 한 대**를 띄운다. 개발 PC에서 실행한다.
#
#   bash launch-builder.sh              # 띄운다(확인 프롬프트 있음)
#   bash launch-builder.sh terminate    # 다 끝난 뒤 치운다
#
# 이 인스턴스는 ASG 바깥의 맨 인스턴스다. 그래서 **아무도 회수해주지 않는다** —
# 리퍼는 컴포즈 스택 안에 있으니 여기엔 없고, 가디언은 ASG 멤버만 물러나게 한다.
# 켜 둔 채로 자면 그대로 요금이 나간다. 끝나면 반드시 `terminate` 를 돌릴 것.
#
# ── 왜 스팟인가 ─────────────────────────────────────────────────────────
# 승인된 쿼터가 "G/VT **스팟** 8 vCPU"다. 온디맨드 G 쿼터는 별도 항목이고 0일
# 수 있어서, 확실히 뜨는 쪽을 쓴다. g6.xlarge 는 4 vCPU라 8 안에 들어가고,
# 빌드하는 동안 ASG는 0대라 쿼터가 통째로 비어 있다.
#
# 중단되면 루트 볼륨도 같이 사라진다(DeleteOnTermination=true). 받아둔 85GB가
# 날아가니 처음부터 다시다. 볼륨을 남기는 선택지도 있지만, 그러면 인스턴스가
# 사라진 뒤에도 150GB가 조용히 과금되는 쪽이 더 위험하다 — 이 프로젝트에서
# 제일 경계하는 누수 형태다. 대신 bootstrap-ami.sh 가 조각 단위로 재개한다.
#
# ── 반드시 tmux 안에서 bootstrap 을 돌릴 것 ──────────────────────────────
# 받는 데 1~2시간 걸린다. 그냥 ssh로 돌리면 노트북이 절전되거나 wifi가 한 번
# 끊기는 순간 프로세스가 같이 죽는다. 아래 안내 문구가 tmux 명령을 찍어준다.
set -euo pipefail
cd "$(dirname "$0")"
. ./config.sh

BUILDER_NAME=${EMR_BUILDER_NAME:-emr-ami-builder}
SG_NAME=${EMR_BUILDER_SG:-emr-builder-sg}
KEY_NAME=${EMR_BUILDER_KEY:-emr-builder-key}
KEY_FILE=${EMR_BUILDER_KEY_FILE:-$HOME/.ssh/$KEY_NAME.pem}
# 4 vCPU 짜리만 둔다. 8 vCPU(2xlarge)를 고르면 쿼터를 한 대로 다 쓴다.
TYPES=${EMR_BUILDER_TYPES:-"g6.xlarge g5.xlarge g6e.xlarge"}

tag_filter=(--filters "Name=tag:Name,Values=$BUILDER_NAME"
            "Name=instance-state-name,Values=pending,running,stopping,stopped")
find_builder() {
  aws ec2 describe-instances "${tag_filter[@]}" \
    --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null | tr -s '[:space:]' ' '
}

# ── terminate 모드 ───────────────────────────────────────────────────────
if [ "${1:-}" = "terminate" ]; then
  IDS=$(find_builder)
  if [ -z "${IDS// /}" ]; then
    echo "▶ 살아있는 빌더가 없습니다."
  else
    echo "▶ 종료: $IDS"
    aws ec2 terminate-instances --instance-ids $IDS \
      --query 'TerminatingInstances[].[InstanceId,CurrentState.Name]' --output text
    echo "  볼륨은 DeleteOnTermination=true 라 같이 사라집니다."
    echo "  (확인: aws ec2 describe-volumes --filters Name=tag:Project,Values=emr Name=status,Values=available)"
  fi
  # 보안그룹은 인스턴스가 완전히 사라져야 지워진다. 실패해도 치명적이지 않다.
  if SG=$(aws ec2 describe-security-groups --group-names "$SG_NAME" \
            --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null); then
    echo "▶ 보안그룹 $SG_NAME 정리 시도 (인스턴스가 완전히 종료된 뒤에만 됩니다)"
    aws ec2 delete-security-group --group-id "$SG" 2>/dev/null \
      && echo "  삭제됨" \
      || echo "  아직 사용 중 — 1~2분 뒤 다시: aws ec2 delete-security-group --group-id $SG"
  fi
  exit 0
fi

# ── 0. 이미 떠 있나 ──────────────────────────────────────────────────────
EXISTING=$(find_builder)
if [ -n "${EXISTING// /}" ]; then
  echo "✗ 빌더가 이미 있습니다: $EXISTING"
  echo "  이어서 작업하려면 접속만 하세요. 치우려면: bash $0 terminate"
  exit 1
fi

# ── 1. 전제 확인 ─────────────────────────────────────────────────────────
echo "▶ 전제 확인"
command -v aws >/dev/null || { echo "✗ aws CLI 없음"; exit 1; }
ACCT=$(aws sts get-caller-identity --query Account --output text) \
  || { echo "✗ AWS 자격증명 없음 — aws configure 를 먼저 하세요"; exit 1; }
echo "  계정 ${ACCT:0:4}… / 리전 $AWS_REGION"

aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null 2>&1 \
  || { echo "✗ 인스턴스 프로필 $PROFILE_NAME 없음 — ./10-iam.sh 를 먼저 돌리세요"; exit 1; }
echo "  인스턴스 프로필 $PROFILE_NAME"

# 매니페스트가 S3에 있어야 부트스트랩이 돌아간다. 없는 채로 띄우면 비싼
# GPU 인스턴스를 켜 놓고 "pack-envs.sh 부터 돌리세요"를 읽게 된다.
PREFIX=${EMR_AMI_PREFIX:-ami/v1}
aws s3api head-object --bucket "$S3_BUCKET" --key "$PREFIX/manifest.json" >/dev/null 2>&1 \
  || { echo "✗ s3://$S3_BUCKET/$PREFIX/manifest.json 이 없습니다."
       echo "  개발 PC에서 ./pack-envs.sh 를 먼저 끝내세요(약 51GB 업로드)."; exit 1; }
# 버킷 수명주기가 7일이라 포장이 오래됐으면 조각이 이미 지워졌을 수 있다.
AGE_D=$(( ( $(date +%s) - $(date -d "$(aws s3api head-object --bucket "$S3_BUCKET" \
          --key "$PREFIX/manifest.json" --query LastModified --output text)" +%s) ) / 86400 ))
echo "  매니페스트 있음 (${AGE_D}일 전 업로드)"
[ "$AGE_D" -ge 6 ] && echo "  ! 버킷 수명주기가 7일입니다 — 조각이 지워졌을 수 있으니 pack-envs.sh 를 다시 돌리세요"

# ── 2. 기반 AMI ──────────────────────────────────────────────────────────
# 드라이버 + 도커 + nvidia-container-toolkit 만 든 Base DLAMI. 프레임워크가
# 들어간 일반 DLAMI는 우리가 안 쓰는 PyTorch를 수십 GB 들고 다니게 된다.
echo "▶ 기반 AMI 조회"
SSM_PARAM=/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id
BASE_AMI=$(aws ssm get-parameter --name "$SSM_PARAM" --query 'Parameter.Value' --output text 2>/dev/null || echo "")
if [ -z "$BASE_AMI" ] || [ "$BASE_AMI" = "None" ]; then
  # SSM 별칭은 AWS가 이름을 바꾸기도 한다. 이름으로 직접 찾는 길을 남겨둔다.
  BASE_AMI=$(aws ec2 describe-images --owners amazon \
    --filters "Name=name,Values=Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*" \
              "Name=state,Values=available" \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
fi
[ -n "$BASE_AMI" ] && [ "$BASE_AMI" != "None" ] \
  || { echo "✗ 기반 AMI를 찾지 못했습니다. 콘솔에서 AMI ID를 찾아 EMR_BASE_AMI=ami-xxxx 로 넘기세요"; exit 1; }
BASE_AMI=${EMR_BASE_AMI:-$BASE_AMI}
AMI_NAME=$(aws ec2 describe-images --image-ids "$BASE_AMI" --query 'Images[0].Name' --output text)
# 루트 장치 이름을 추측하지 않는다. 틀리면 EC2는 에러 없이 **추가 볼륨**을 하나
# 더 붙이고, 루트는 기본 크기 그대로 남아 부트스트랩이 디스크 부족으로 죽는다.
ROOT_DEV=$(aws ec2 describe-images --image-ids "$BASE_AMI" --query 'Images[0].RootDeviceName' --output text)
echo "  $BASE_AMI ($AMI_NAME)"
echo "  루트 장치 $ROOT_DEV"

# ── 3. 키페어 ────────────────────────────────────────────────────────────
echo "▶ 키페어"
if aws ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
  [ -f "$KEY_FILE" ] || { echo "✗ AWS에는 키 $KEY_NAME 이 있는데 로컬 $KEY_FILE 이 없습니다."
                          echo "  개인키는 생성 시 한 번만 받을 수 있습니다. 지우고 다시 만드세요:"
                          echo "    aws ec2 delete-key-pair --key-name $KEY_NAME && bash $0"; exit 1; }
  echo "  기존 키 재사용: $KEY_FILE"
else
  mkdir -p "$(dirname "$KEY_FILE")"
  # umask 로 먼저 막는다. 만들고 나서 chmod 하면 그 사이에 다른 사용자가 읽을 수
  # 있다 — 이 머신은 3명이 같이 쓴다.
  ( umask 077
    aws ec2 create-key-pair --key-name "$KEY_NAME" \
      --query 'KeyMaterial' --output text > "$KEY_FILE" )
  chmod 600 "$KEY_FILE"
  echo "  새 키 생성: $KEY_FILE (600)"
  echo "  ! 이 파일은 저장소에 커밋하지 마세요(*.pem 은 .gitignore 에 있어야 합니다)"
fi

# ── 4. 네트워크 ──────────────────────────────────────────────────────────
echo "▶ 네트워크"
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
        --query 'Vpcs[0].VpcId' --output text)
[ "$VPC" != "None" ] || { echo "✗ 기본 VPC가 없습니다. EMR_BUILDER_SUBNET 으로 서브넷을 지정하세요"; exit 1; }

# 내 공인 IP만 SSH를 연다. 0.0.0.0/0 으로 열면 봇이 몇 분 안에 붙기 시작한다.
MYIP=$(curl -s --max-time 10 https://checkip.amazonaws.com || echo "")
SSH_CIDR=${EMR_SSH_CIDR:-${MYIP:+$MYIP/32}}
[ -n "$SSH_CIDR" ] || { echo "✗ 공인 IP를 못 읽었습니다. EMR_SSH_CIDR=1.2.3.4/32 로 지정하세요"; exit 1; }

if SG=$(aws ec2 describe-security-groups --group-names "$SG_NAME" \
          --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null); then
  echo "  기존 보안그룹 $SG_NAME ($SG)"
else
  SG=$(aws ec2 create-security-group --group-name "$SG_NAME" --vpc-id "$VPC" \
        --description "EMR AMI builder - SSH only, temporary" \
        --query 'GroupId' --output text)
  aws ec2 create-tags --resources "$SG" --tags Key=Project,Value=emr
  echo "  보안그룹 생성 $SG"
fi
# 규칙은 매번 넣어본다. IP가 바뀌었을 수 있다(중복이면 조용히 넘어간다).
aws ec2 authorize-security-group-ingress --group-id "$SG" \
  --protocol tcp --port 22 --cidr "$SSH_CIDR" >/dev/null 2>&1 \
  && echo "  SSH 허용 추가: $SSH_CIDR" \
  || echo "  SSH 허용 이미 있음: $SSH_CIDR"

# 공인 IP가 붙는 서브넷만 쓴다(SSH로 들어가야 하므로).
# EMR_BUILDER_SUBNET 을 주면 그 하나만 쓴다. 위 142줄 오류 메시지가 이 변수를
# 안내하면서 정작 읽지는 않고 있었다. AZ 를 고정하고 싶을 때 쓴다.
if [ -n "${EMR_BUILDER_SUBNET:-}" ]; then
  SUBNETS=$(aws ec2 describe-subnets --subnet-ids "$EMR_BUILDER_SUBNET" \
              --query 'Subnets[].[SubnetId,AvailabilityZone]' --output text)
else
  SUBNETS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" \
              "Name=map-public-ip-on-launch,Values=true" \
              --query 'Subnets[].[SubnetId,AvailabilityZone]' --output text)
fi
[ -n "$SUBNETS" ] || { echo "✗ 퍼블릭 서브넷이 없습니다"; exit 1; }
echo "  후보 AZ: $(echo "$SUBNETS" | awk '{printf "%s ", $2}')"

# MaxPrice 를 안 준다 = 온디맨드 가격까지 허용. 낮게 박으면 "싸게 뜨는" 게 아니라
# **아예 안 뜬다**(스팟은 어차피 시장가로 과금된다).
#
# 빌더만은 온디맨드로 돌릴 길을 열어둔다. 워커 ASG 와 사정이 다르다 —
# 워커는 회수되면 다음 놈이 큐에서 집어가지만, 빌더는 ASG 밖의 일회성
# 장비라 받아줄 사람이 없다. 회수되는 순간 S3 에서 48GB 복원한 게 통째로
# 날아가고 1~2시간을 다시 기다린다. 실제로 2026-10-06 에 띄운 지 8분 만에
# instance-terminated-no-capacity 로 잃었고, 그때 2b/2c 는 애초에 거부당해
# 2a 한 곳에만 떠 있던 상태였다. 아끼는 건 시간당 $0.51 인데 거는 건 두 시간이다.

MKT='{"MarketType":"spot","SpotOptions":{"SpotInstanceType":"one-time","InstanceInterruptionBehavior":"terminate"}}'
MKT_LABEL="스팟"
MKT_FEE="스팟 g6.xlarge 약 \$0.48/h — 4시간이면 \$2 안팎 (중간 회수 위험 있음)"
# ASG 밖이라 '아무도 회수 안 한다'고 적어뒀었는데 틀린 말이었다. ASG 는 안
# 건드려도 AWS 가 스팟 용량을 회수한다. 2026-10-06 에 8분 만에 당했다.
MKT_WARN="스팟입니다 — AWS 가 용량 회수로 언제든 내릴 수 있고, 그러면 복원을 처음부터 다시 합니다."
if [ "${EMR_BUILDER_MARKET:-spot}" = "ondemand" ]; then
  # 빈 MKT 는 아래 run-instances 에서 --instance-market-options 자체를 빼는 신호다.
  MKT=""
  MKT_LABEL="온디맨드"
  MKT_FEE="온디맨드 g6.xlarge \$0.99/h — 4시간이면 \$4 안팎 (회수 없음)"
  MKT_WARN="온디맨드라 회수되지 않습니다. 대신 끄기 전까지 계속 과금됩니다."
fi

# ── 5. 확인 ──────────────────────────────────────────────────────────────
cat <<PLAN

────────────────────────────────────────────────────────────
  띄울 것          $MKT_LABEL 인스턴스 1대 ($(echo $TYPES | tr ' ' '/') 중 뜨는 것)
  루트 볼륨        ${EMR_VOLUME_GB}GB gp3 ${EMR_VOLUME_THROUGHPUT}MB/s (종료 시 삭제)
  기반 AMI         $BASE_AMI
  역할             $PROFILE_NAME
  SSH              $SSH_CIDR 에서만
  대략 요금        $MKT_FEE
────────────────────────────────────────────────────────────
  $MKT_WARN
  끝나면 반드시:  bash $0 terminate
────────────────────────────────────────────────────────────

PLAN
if [ "${EMR_YES:-}" != "1" ]; then
  read -r -p "띄울까요? (yes 입력) " a
  [ "$a" = "yes" ] || { echo "취소했습니다."; exit 1; }
fi

# ── 6. 띄우기 ────────────────────────────────────────────────────────────
# 루트 볼륨 크기를 꼭 덮어쓴다. 기본 AMI의 크기를 그대로 받으면 85GB가 안 들어가고,
# **스냅샷 크기가 곧 EMR_VOLUME_GB의 하한**이라 여기서 150을 벗어나면 나중에
# 20-launch-template.sh 가 거부한다.
BDM="[{\"DeviceName\":\"$ROOT_DEV\",\"Ebs\":{\"VolumeSize\":$EMR_VOLUME_GB,\"VolumeType\":\"gp3\",\"Throughput\":$EMR_VOLUME_THROUGHPUT,\"DeleteOnTermination\":true}}]"
IID=""
for t in $TYPES; do
  while read -r subnet az; do
    [ -n "$subnet" ] || continue
    echo "▶ 시도: $t / $az"
    if IID=$(aws ec2 run-instances \
        --image-id "$BASE_AMI" --instance-type "$t" --count 1 \
        --key-name "$KEY_NAME" --subnet-id "$subnet" --security-group-ids "$SG" \
        --iam-instance-profile "Name=$PROFILE_NAME" \
        --block-device-mappings "$BDM" \
        ${MKT:+--instance-market-options "$MKT"} \
        --metadata-options 'HttpTokens=required,HttpPutResponseHopLimit=2' \
        --tag-specifications \
          "ResourceType=instance,Tags=[{Key=Name,Value=$BUILDER_NAME},{Key=Project,Value=emr}]" \
          "ResourceType=volume,Tags=[{Key=Project,Value=emr}]" \
        --query 'Instances[0].InstanceId' --output text 2>/tmp/emr-launch-err); then
      echo "  ✔ 떴습니다: $IID ($t / $az)"
      break 2
    fi
    echo "  실패: $(tail -1 /tmp/emr-launch-err | cut -c1-160)"
  done <<< "$SUBNETS"
done
[ -n "$IID" ] || { echo
  echo "✗ 모든 타입/AZ 에서 실패했습니다. 위 메시지를 보세요:"
  echo "  InsufficientInstanceCapacity → 스팟 풀이 빈 것. 잠시 뒤 다시."
  echo "  VcpuLimitExceeded            → 쿼터. ASG가 0대인지 확인하세요."
  echo "  UnauthorizedOperation        → emr-deploy 사용자에게 ec2:RunInstances 권한이 필요합니다."
  exit 1; }

echo "▶ running 대기"
aws ec2 wait instance-running --instance-ids "$IID"
IP=$(aws ec2 describe-instances --instance-ids "$IID" \
      --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

cat <<NEXT

✔ 빌더 준비 완료 — $IID / $IP

  1) 접속 (sshd가 뜰 때까지 20~30초 걸릴 수 있습니다)
       ssh -i $KEY_FILE ubuntu@$IP

  2) **반드시 tmux 안에서** 부트스트랩을 돌립니다. 1~2시간 걸리는데,
     그냥 돌리면 SSH가 한 번 끊길 때 같이 죽습니다.
       tmux new -s build
       git clone https://github.com/simu1231/Empty_My_Room.git /tmp/emr-repo
       sudo EMR_AMI_PREFIX=$PREFIX bash /tmp/emr-repo/deploy/aws/bootstrap-ami.sh
     (떨어졌으면 재접속 후 \`tmux attach -t build\`. 끊긴 조각은 다시 받고
      끝난 조각은 건너뜁니다.)

  3) 이미지 빌드 → 실동작 → 검증. **한 줄씩** 따로 돌리고 출력을 봅니다
       cd $EMR_REPO_DIR/deploy/aws
       bash bake-ami.sh     # 세 이미지를 git SHA 태그로 빌드
       bash smoke-test.sh   # 스택을 띄워 세 모델에 진짜 작업을 통과시킨다
       bash verify-ami.sh   # AMI가 되기 위한 조건 점검

  4) 통과하면 **개발 PC에서** 스냅샷을 찍습니다
       aws ec2 create-image --instance-id $IID \\
         --name emr-gpu-\$(date +%Y%m%d-%H%M) --no-reboot

  5) available 이 되면 즉시 치웁니다 (아무도 회수해주지 않습니다)
       aws ec2 wait image-available --image-ids ami-xxxx
       bash $0 terminate

NEXT
