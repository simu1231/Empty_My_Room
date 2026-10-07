#!/usr/bin/env bash
# API 서버(t4g.small) 한 대를 띄운다. ASG 도 시작 템플릿도 쓰지 않는다 —
# 상시 1대 고정이라 스케일 개념이 없다. 그래서 20/30/40 에 끼워 넣지 않고
# 번호를 뒤에 붙여 분리했다. GPU 경로 스크립트는 한 줄도 건드리지 않는다.
#
#   bash deploy/aws/50-api.sh            # 계획만 출력(기본) — 아무것도 안 만든다
#   DRY_RUN=0 bash deploy/aws/50-api.sh  # 실제로 보안그룹 생성 + 인스턴스 기동
#
# 기본값이 DRY_RUN=1 인 이유: 이 스크립트가 만드는 건 **시간당 과금되는 인스턴스**다.
# 워커는 리퍼가 120초 뒤 회수하지만 API 는 설계상 영원히 안 내려간다. 실수로
# 한 번 돌렸을 때 조용히 월 $19.7 이 시작되는 것보다, 한 번 더 치는 쪽이 싸다.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./config.sh

DRY_RUN=${DRY_RUN:-1}
run() {  # DRY_RUN 일 때는 실행하지 않고 보여만 준다
  if [ "$DRY_RUN" = "0" ]; then "$@"; else echo "    [계획] $*"; fi
}

# 보안그룹 규칙 전용. run() 과 갈라놓은 이유는 **재실행 때문**이다.
# authorize-security-group-ingress 는 같은 규칙을 두 번 넣으면
# InvalidPermission.Duplicate 로 실패하고, set -e 가 스크립트를 거기서 죽인다.
# 그래서 이 파일은 보안그룹이 없는 **첫 실행에서만** 통과했다. API 인스턴스를
# 교체할 때마다(코드만 바꿔 다시 띄우는 게 정상 운영이다) 같은 SG 를 재사용하므로
# 두 번째부터는 항상 여기서 멈춘다 — 아래 워커 8001 블록이 이미 같은 이유로
# 중복을 삼키고 있는데, 이 두 줄만 빠져 있었다.
run_sg() {
  if [ "$DRY_RUN" != "0" ]; then echo "    [계획] $*"; return 0; fi
  local err
  err=$("$@" 2>&1 >/dev/null) && return 0
  case "$err" in
    *InvalidPermission.Duplicate*) echo "  (이미 있음)"; return 0 ;;
    *) echo "$err" >&2; return 1 ;;
  esac
}

echo "▶ 사전 점검"

# 인스턴스 프로파일. 10-iam.sh 가 만든다. 없으면 여기서 멈춘다 —
# 프로파일 없이 띄우면 인스턴스는 멀쩡히 뜨고 첫 요청에서 AccessDenied 가 난다.
aws iam get-instance-profile --instance-profile-name "$API_PROFILE_NAME" >/dev/null 2>&1 || {
  echo "✗ 인스턴스 프로파일 $API_PROFILE_NAME 이 없습니다 — 먼저 10-iam.sh 를 돌리세요"; exit 1; }
echo "  프로파일 $API_PROFILE_NAME ✔"

# 버킷 이름이 추측값이면 멈춘다. docker-compose.aws.yml 머리말의 그 사고다 —
# 이름이 틀리면 전 작업이 HeadObject 404 로 죽는데 기동은 멀쩡히 성공한다.
[ "${EMR_BUCKET_GUESSED:-0}" = "0" ] || {
  echo "✗ S3_BUCKET($S3_BUCKET) 이 추측값입니다. config.sh 에서 확정하세요"; exit 1; }

# 이미 떠 있으면 두 번째를 만들지 않는다. 상시 가동이라 중복분이 그대로
# 월 $19.7 추가다. 멱등성은 여기서 지켜야 한다 — ASG 가 없으니 대신 세어 줄
# 주체도 없다.
EXIST=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=$API_NAME" \
            "Name=instance-state-name,Values=running,pending" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
[ -z "$EXIST" ] || {
  echo "✗ $API_NAME 이 이미 떠 있습니다: $EXIST"
  echo "   교체하려면 먼저 종료하세요: aws ec2 terminate-instances --instance-ids $EXIST"; exit 1; }
echo "  기존 $API_NAME 인스턴스 없음 ✔"

# ── AMI ──
# ssm:GetParameter 가 AccessDenied 라 AL2023 공식 파라미터 경로를 못 쓴다.
# 이름 패턴으로 직접 뒤지고 CreationDate 역순 첫 번째를 쓴다.
# arm64 를 반드시 확인한다 — x86 AMI 를 t4g 에 주면 run-instances 자체가
# 거부되지만, 그 에러 메시지만으로는 원인이 바로 안 보인다.
if [ -z "${API_AMI_ID:-}" ]; then
  API_AMI_ID=$(aws ec2 describe-images --owners amazon \
    --filters "Name=name,Values=$API_AMI_PATTERN" "Name=state,Values=available" \
              "Name=architecture,Values=arm64" \
    --query 'reverse(sort_by(Images,&CreationDate))[0].ImageId' --output text)
fi
[ -n "$API_AMI_ID" ] && [ "$API_AMI_ID" != "None" ] || {
  echo "✗ arm64 AMI 를 못 찾았습니다 (패턴: $API_AMI_PATTERN)"; exit 1; }
ARCH=$(aws ec2 describe-images --image-ids "$API_AMI_ID" \
         --query 'Images[0].Architecture' --output text)
[ "$ARCH" = "arm64" ] || { echo "✗ AMI $API_AMI_ID 아키텍처가 $ARCH 입니다 — t4g 는 arm64"; exit 1; }
echo "  AMI $API_AMI_ID ($ARCH) ✔"

# ── 서브넷 ──
# 퍼블릭 IP 가 자동 할당되는 서브넷이어야 한다. 아니면 인스턴스는 뜨는데
# 밖에서 닿지 않고, 인터넷으로 나가지도 못해 git clone 부터 막힌다.
if [ -z "${API_SUBNET_ID:-}" ]; then
  API_SUBNET_ID=$(aws ec2 describe-subnets \
    --filters "Name=map-public-ip-on-launch,Values=true" \
    --query 'Subnets[0].SubnetId' --output text)
fi
[ -n "$API_SUBNET_ID" ] && [ "$API_SUBNET_ID" != "None" ] || {
  echo "✗ 퍼블릭 서브넷을 못 찾았습니다"; exit 1; }
VPC_ID=$(aws ec2 describe-subnets --subnet-ids "$API_SUBNET_ID" \
           --query 'Subnets[0].VpcId' --output text)
echo "  서브넷 $API_SUBNET_ID (VPC $VPC_ID) ✔"

# ── SSH 허용 범위 ──
MYIP=$(curl -s --max-time 10 https://checkip.amazonaws.com || echo "")
SSH_CIDR=${API_SSH_CIDR:-${MYIP:+$MYIP/32}}
[ -n "$SSH_CIDR" ] || { echo "✗ 공인 IP 를 못 읽었습니다. API_SSH_CIDR=1.2.3.4/32 로 지정하세요"; exit 1; }
aws ec2 describe-key-pairs --key-names "$API_KEY_NAME" >/dev/null 2>&1 || {
  echo "✗ 키페어 $API_KEY_NAME 이 AWS 에 없습니다"; exit 1; }
echo "  SSH $SSH_CIDR / 키 $API_KEY_NAME ✔"

# ── 보안그룹 ──
# 워커 SG(인바운드 0개)를 돌려쓰면 안 된다. API 는 밖에서 들어와야 하고,
# 그 규칙을 워커에 얹으면 GPU 인스턴스까지 같이 열린다.
echo "▶ 보안그룹 $API_SG_NAME"
SG_ID=$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=$API_SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")
if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
  if [ "$DRY_RUN" = "0" ]; then
    SG_ID=$(aws ec2 create-security-group --group-name "$API_SG_NAME" \
      --description "Empty My Room - Job API" --vpc-id "$VPC_ID" \
      --tag-specifications 'ResourceType=security-group,Tags=[{Key=Project,Value=emr}]' \
      --query 'GroupId' --output text)
    echo "  생성: $SG_ID"
  else
    SG_ID="(생성 예정)"; echo "    [계획] create-security-group $API_SG_NAME in $VPC_ID"
  fi
else
  echo "  기존 사용: $SG_ID"
fi
# 22 는 내 IP 만. 8000 은 전체 — 프런트(브라우저)가 직접 부르는 주소라
# 소스를 좁힐 수 없다. TLS/도메인 단계에서 443 으로 옮기고 여기를 닫는다.
run_sg aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
      --protocol tcp --port 22 --cidr "$SSH_CIDR"
run_sg aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
      --protocol tcp --port "$API_PORT" --cidr 0.0.0.0/0

# ── 워커 → API 연결 통로 ────────────────────────────────────────────────
# API 서버가 GPU 워커의 :8001 로 요청을 중계한다(gpuproxy.py). 그러려면 워커
# 보안그룹이 8001 을 열어야 하는데, **CIDR 이 아니라 SG 참조로** 연다.
#
# 왜 SG 참조인가: 워커는 스케일투제로라 뜰 때마다 IP 가 바뀐다. CIDR 로 열면
# 그 IP 를 매번 갱신하거나, 귀찮아서 0.0.0.0/0 으로 열게 된다. SG 참조는
# "emr-api-sg 가 붙은 인스턴스"를 가리키므로 IP 가 바뀌어도 그대로 맞는다.
#
# 8002(uLayout)/8003(Omni3D)은 **열지 않는다.** 백엔드가 컨테이너 네트워크
# 안에서 부르는 포트지 바깥에서 부를 포트가 아니다.
if [ -n "${EMR_SG_ID:-}" ]; then
  echo "▶ 워커 보안그룹에 8001 허용 (출발지: $SG_ID)"
  # 규칙 설명은 ASCII 만 된다. 한글을 넣으면 InvalidParameterValue 로 거부된다
  # (허용 집합: a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*).
  #
  # 주석을 명령 **위**에 둔다. 줄 이음(\) 다음 줄이 # 으로 시작하면 쉘이 거기서
  # 명령을 끊어버려서, --ip-permissions 가 통째로 사라진 채
  # `aws ... --group-id X` 만 실행된다. MissingParameter 로 떨어지는데 뒤에 붙은
  # `|| echo "(이미 있음)"` 까지 같이 떨어져 나가서 에러가 그대로 새어 나온다.
  IP_PERM="IpProtocol=tcp,FromPort=${GPU_PORT},ToPort=${GPU_PORT},UserIdGroupPairs=[{GroupId=$SG_ID,Description='emr-api-sg proxy to backend 8001'}]"
  run_sg aws ec2 authorize-security-group-ingress \
    --group-id "$EMR_SG_ID" --ip-permissions "$IP_PERM"
else
  echo "⚠ EMR_SG_ID 가 없어 워커 8001 규칙을 건너뜁니다."
  echo "  이 상태로 두면 API 가 워커를 찾아도 연결이 타임아웃됩니다:"
  echo "    EMR_SG_ID=sg-xxxx bash $0"
fi

# ── 유저데이터 ──
# config.sh 의 값을 치환해 넣는다. 워커와 같은 방식이고, 같은 16KB 한도를 받는다
# (run-instances 도 한도가 같다). 지금은 4.3KB 라 여유가 넉넉하다.
echo "▶ 유저데이터 렌더"
sed -e "s|__REPO_URL__|$API_REPO_URL|g" \
    -e "s|__GIT_REF__|$API_GIT_REF|g" \
    -e "s|__EMR_ROOT__|$EMR_ROOT|g" \
    -e "s|__PORT__|$API_PORT|g" \
    -e "s|__REGION__|$AWS_REGION|g" \
    -e "s|__S3_BUCKET__|$S3_BUCKET|g" \
    -e "s|__DDB_TABLE__|$DDB_TABLE|g" \
    -e "s|__Q_SAM3D__|$Q_SAM3D|g" \
    -e "s|__Q_SCENE__|$Q_SCENE|g" \
    -e "s|__ASG_SPOT__|$ASG_SPOT|g" \
    -e "s|__CORS_ORIGINS__|$CORS_ORIGINS|g" \
    -e "s|__NUDGE__|$CAPACITY_NUDGE_INTERVAL|g" \
    -e "s|__PROJECT__|${PROJECT:-emr}|g" \
    -e "s|__GPU_PORT__|${GPU_PORT:-8001}|g" \
    -e "s|__PREWARM_RATE__|${PREWARM_RATE:-30}|g" \
    ./api-userdata.sh > /tmp/emr-api-userdata.rendered.sh
grep -q '__[A-Z0-9_]*__' /tmp/emr-api-userdata.rendered.sh && {
  echo "✗ 치환되지 않은 자리표시자가 남았습니다:"
  grep -o '__[A-Z0-9_]*__' /tmp/emr-api-userdata.rendered.sh | sort -u; exit 1; }
bash -n /tmp/emr-api-userdata.rendered.sh || { echo "✗ 렌더된 유저데이터 문법 오류"; exit 1; }
RAW=$(wc -c < /tmp/emr-api-userdata.rendered.sh)
[ "$RAW" -le 16384 ] || { echo "✗ 유저데이터 ${RAW} 바이트 — 16384 한도 초과"; exit 1; }
echo "  ${RAW}/16384 바이트 ✔"

# ── 기동 ──
cat <<SUMMARY

────────── 기동 계획 ──────────
  이름           $API_NAME
  타입           $API_INSTANCE_TYPE (arm64, 온디맨드)
  AMI            $API_AMI_ID
  서브넷         $API_SUBNET_ID
  보안그룹       $SG_ID  (22←$SSH_CIDR, $API_PORT←0.0.0.0/0)
  프로파일       $API_PROFILE_NAME
  루트 볼륨      ${API_VOLUME_GB}GB gp3 (DeleteOnTermination=true)
  저장소         $API_REPO_URL @ $API_GIT_REF
  큐/버킷        $Q_SAM3D, $Q_SCENE / $S3_BUCKET / $DDB_TABLE
  깨울 ASG       $ASG_SPOT
  CORS           $CORS_ORIGINS
  예상 비용      시간당 \$0.0208 + 퍼블릭 IPv4 \$0.005 ≈ 월 \$19.7
───────────────────────────────
SUMMARY

if [ "$DRY_RUN" != "0" ]; then
  echo "DRY_RUN=1 입니다. 아무것도 만들지 않았습니다."
  echo "실제로 띄우려면: DRY_RUN=0 bash deploy/aws/50-api.sh"
  exit 0
fi

echo "▶ 인스턴스 기동"
# DeleteOnTermination=true 를 명시한다. 기본값은 AMI 가 정하는데, 거기 false 가
# 들어 있으면 인스턴스를 지워도 볼륨만 남아 영원히 과금된다.
IID=$(aws ec2 run-instances \
  --image-id "$API_AMI_ID" --instance-type "$API_INSTANCE_TYPE" \
  --key-name "$API_KEY_NAME" --subnet-id "$API_SUBNET_ID" \
  --security-group-ids "$SG_ID" \
  --iam-instance-profile "Name=$API_PROFILE_NAME" \
  --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=$API_VOLUME_GB,VolumeType=gp3,DeleteOnTermination=true}" \
  --metadata-options "HttpTokens=required,HttpPutResponseHopLimit=2" \
  --user-data "file:///tmp/emr-api-userdata.rendered.sh" \
  --tag-specifications \
    "ResourceType=instance,Tags=[{Key=Name,Value=$API_NAME},{Key=Project,Value=emr}]" \
    "ResourceType=volume,Tags=[{Key=Project,Value=emr}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "  $IID"

aws ec2 wait instance-running --instance-ids "$IID"
IP=$(aws ec2 describe-instances --instance-ids "$IID" \
       --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

cat <<DONE

✔ 기동됨: $IID  /  $IP

빌드(git clone + docker build)에 2~4분 걸린다. 그 전까지 /health 는 응답하지 않는다.

  확인:  curl http://$IP:$API_PORT/health
  로그:  ssh -i ~/.ssh/$API_KEY_NAME.pem ec2-user@$IP \\
           'sudo journalctl -u emr-api -n 50; sudo tail -50 /var/log/emr-api-userdata.log'
  종료:  aws ec2 terminate-instances --instance-ids $IID
DONE
