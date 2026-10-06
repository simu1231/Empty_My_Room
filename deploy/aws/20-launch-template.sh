#!/bin/bash
# 시작 템플릿 — "인스턴스 한 대를 어떻게 만들 것인가"의 설계도.
#
# ASG가 인스턴스를 띄울 때마다 이걸 읽는다. 인스턴스 타입은 여기 적지 않는다.
# 타입은 ASG의 혼합 인스턴스 정책이 후보 목록에서 그때그때 고르기 때문이다.
set -euo pipefail
cd "$(dirname "$0")" && source ./config.sh

: "${EMR_AMI_ID:?AMI ID를 지정하세요: EMR_AMI_ID=ami-xxxx $0}"
: "${EMR_SG_ID:?보안그룹 ID를 지정하세요: EMR_SG_ID=sg-xxxx $0}"

# ── 이미지 태그는 **AMI에서** 읽는다 ────────────────────────
# config.sh 의 EMR_IMAGE_TAG 기본값은 **지금 작업 중인 저장소의 git HEAD** 다.
# 그런데 워커가 쓸 이미지는 **AMI를 굽던 시점의** 태그로 박혀 있다. 굽고 나서
# 커밋을 하나만 더 해도 둘이 어긋나고, 그러면 시작 템플릿에 **AMI에 없는**
# 태그가 박힌다. 워커는 --no-build 라 그 자리에서 부팅을 거부한다.
#
# 배포하려는 AMI 가 정답을 들고 있으므로(bake 때 GitSha 태그를 붙였다)
# 작업 디렉터리 상태를 묻지 말고 거기서 읽는다. 명령줄로 준 EMR_IMAGE_TAG 는
# 그대로 존중한다(수동 복구용).
#
# aws 가 PATH 에 없는 경우를 먼저 가른다. 아래 호출을 2>/dev/null 로 감싸 놨으니
# CLI 부재도 "태그가 비었다"로 흘러들어, 엉뚱하게 "AMI 에 GitSha 태그가
# 없습니다"를 찍는다. 그 메시지를 믿고 AMI 태그를 들여다보면 아무 문제가 없어서
# 한참 헤맨다. 원인과 메시지를 일치시킨다.
command -v aws >/dev/null 2>&1 || {
  echo "✗ aws CLI 를 찾을 수 없습니다 (PATH=$PATH)"
  echo "  이 PC 에서는 ~/.local/bin 에 있다: export PATH=\"\$HOME/.local/bin:\$PATH\""
  exit 1
}
AMI_SHA=$(aws ec2 describe-images --image-ids "$EMR_AMI_ID" \
           --query "Images[0].Tags[?Key=='GitSha'].Value | [0]" --output text 2>/dev/null || echo "")
if [ -n "${EMR_IMAGE_TAG_OVERRIDE:-}" ]; then
  EMR_IMAGE_TAG=$EMR_IMAGE_TAG_OVERRIDE
  echo "▶ 태그 $EMR_IMAGE_TAG (명령줄 지정)"
elif [ -n "$AMI_SHA" ] && [ "$AMI_SHA" != "None" ]; then
  if [ "$AMI_SHA" != "$EMR_IMAGE_TAG" ]; then
    echo "▶ 태그 $AMI_SHA (AMI 기준) — 작업트리는 $EMR_IMAGE_TAG 라 서로 다르다"
    echo "  AMI를 굽고 나서 저장소가 앞서갔다는 뜻이다. 워커는 AMI 안의 이미지를 쓴다."
  else
    echo "▶ 태그 $AMI_SHA (AMI 와 작업트리 일치)"
  fi
  EMR_IMAGE_TAG=$AMI_SHA
else
  echo "✗ AMI $EMR_AMI_ID 에 GitSha 태그가 없습니다."
  echo "  어떤 태그의 이미지가 구워져 있는지 확인한 뒤 명시하세요:"
  echo "    EMR_IMAGE_TAG_OVERRIDE=<sha> EMR_AMI_ID=$EMR_AMI_ID bash $0"
  exit 1
fi

# 세션 알림은 스위치 하나로 양쪽을 동시에 렌더한다 — 한쪽만 켜지는 사고를
# 구조적으로 막는다. 왜 그게 위험한지는 config.sh 의 EMR_BACKEND_SESSION 주석에.
# 끈 경우 상태 파일 경로를 빈 문자열로 넣는다. session_state.py 는 빈 값을
# "꺼짐"으로 읽으므로 자리표시자 하나가 켜짐/꺼짐을 둘 다 표현한다.
EXPECT_WORKERS="$Q_SAM3D,$Q_SCENE"
BACKEND_STATE=""
if [ "${EMR_BACKEND_SESSION:-0}" = "1" ]; then
  EXPECT_WORKERS="$EXPECT_WORKERS,backend"
  BACKEND_STATE="/state/backend.state"
  echo "▶ 세션 알림 켬 — 리퍼가 backend.state 도 본다"
  echo "    꼬리 요금: 마지막 요청 + ${EMR_BACKEND_BUSY_SEC}초 + ${IDLE_EXIT_SEC:-120}초"
else
  echo "▶ 세션 알림 끔 — 2단계 중에도 ${IDLE_EXIT_SEC:-120}초면 회수된다"
fi

echo "▶ 유저데이터 생성 (설정값을 치환해 굽는다)"
sed -e "s|__REGION__|${AWS_REGION}|g" \
    -e "s|__LOG_GROUP__|${LOG_GROUP}|g" \
    -e "s|__S3_BUCKET__|${S3_BUCKET}|g" \
    -e "s|__DDB_TABLE__|${DDB_TABLE}|g" \
    -e "s|__Q_SAM3D__|${Q_SAM3D}|g" \
    -e "s|__Q_SCENE__|${Q_SCENE}|g" \
    -e "s|__IDLE_EXIT_SEC__|${IDLE_EXIT_SEC:-120}|g" \
    -e "s|__EXPECT_WORKERS__|${EXPECT_WORKERS}|g" \
    -e "s|__BACKEND_STATE__|${BACKEND_STATE}|g" \
    -e "s|__BACKEND_BUSY_SEC__|${EMR_BACKEND_BUSY_SEC}|g" \
    -e "s|__REPO_DIR__|${EMR_REPO_DIR}|g" \
    -e "s|__EMR_ROOT__|${EMR_ROOT}|g" \
    -e "s|__IMAGE_TAG__|${EMR_IMAGE_TAG}|g" \
    -e "s|__WARM_JOBS__|${EMR_WARM_JOBS}|g" \
    -e "s|__WARM_SKIP__|${EMR_WARM_SKIP}|g" \
    -e "s|__WARM_BG_DIRS__|${EMR_WARM_BG_DIRS}|g" \
    -e "s|__WARM_BG_TIMEOUT__|${EMR_WARM_BG_TIMEOUT}|g" \
    -e "s|__GUARDIAN_GRACE_SEC__|${GUARDIAN_GRACE_SEC}|g" \
    -e "s|__GUARDIAN_FAIL_MIN__|${GUARDIAN_FAIL_MIN}|g" \
    userdata.sh > /tmp/emr-userdata.rendered.sh

# 치환이 빠지면 인스턴스가 "__IMAGE_TAG__" 라는 태그의 이미지를 찾다 죽는다.
# 부팅 때 죽으면 회수는 되지만(요금은 안 새지만) 원인을 찾으러 인스턴스 로그를
# 뒤져야 한다. 여기서 막는 편이 훨씬 싸다.
if grep -n '__[A-Z_]*__' /tmp/emr-userdata.rendered.sh; then
  echo "✗ 치환되지 않은 자리표시자가 남았습니다(위 줄) — config.sh 를 확인하세요."
  exit 1
fi
bash -n /tmp/emr-userdata.rendered.sh || { echo "✗ 렌더된 유저데이터 문법 오류"; exit 1; }

USERDATA_B64=$(base64 -w0 /tmp/emr-userdata.rendered.sh)
USERDATA_RAW=$(wc -c < /tmp/emr-userdata.rendered.sh)
# EC2 유저데이터 한도는 16KB이고, 기준은 **base64 인코딩 전 원본**이다.
# (AWS 문서: "User data is limited to 16 KB, in raw form, before it is
#  base64-encoded.")
# 처음엔 base64 길이를 16384와 비교했는데, base64 는 3바이트를 4바이트로
# 불리므로 실질 한도가 12KB로 좁아져 있었다 — 한도에 한참 못 미치는 스크립트를
# 거부했다. 한글 주석은 글자당 3바이트라 그 차이가 금방 드러난다.
if [ "$USERDATA_RAW" -gt 16384 ]; then
  echo "✗ 유저데이터 ${USERDATA_RAW} 바이트(원본) — 16384 한도 초과"
  echo "  긴 근거 주석은 config.sh 로 옮길 것. config.sh 는 인스턴스로 안 간다."
  exit 1
fi
echo "  유저데이터 ${USERDATA_RAW}/16384 바이트(원본, base64 ${#USERDATA_B64}), 태그 ${EMR_IMAGE_TAG}"

# ── 루트 볼륨 ────────────────────────────────────────────────────────────
# 지금까지는 AMI가 들고 있는 볼륨 설정을 그대로 물려받았다. 두 가지가 통제 밖이었다:
# 크기(굽던 인스턴스가 어쩌다 가진 값)와 DeleteOnTermination(false면 인스턴스가
# 사라져도 볼륨만 남아 영원히 과금된다 — 스케일투제로에서 제일 조용한 누수다).
#
# 루트 장치 이름을 추측하지 않고 AMI에서 읽는다. 틀리면 EC2는 에러를 내지 않고
# **루트와 별개인 추가 볼륨**을 하나 더 붙인다. 모델은 여전히 느린 루트에서 읽히고
# 요금만 두 배가 된다.
ROOT_DEV=$(aws ec2 describe-images --image-ids "$EMR_AMI_ID" \
             --query 'Images[0].RootDeviceName' --output text)
[ -n "$ROOT_DEV" ] && [ "$ROOT_DEV" != "None" ] || {
  echo "✗ AMI $EMR_AMI_ID 의 루트 장치 이름을 읽지 못했습니다"; exit 1; }

# 스냅샷보다 작은 볼륨은 만들 수 없다. 여기서 안 막으면 ASG 활동 기록에만 남는다.
SNAP_GB=$(aws ec2 describe-images --image-ids "$EMR_AMI_ID" \
            --query "Images[0].BlockDeviceMappings[?DeviceName=='$ROOT_DEV'].Ebs.VolumeSize | [0]" \
            --output text)
if [ "$SNAP_GB" != "None" ] && [ "${EMR_VOLUME_GB}" -lt "$SNAP_GB" ]; then
  echo "✗ EMR_VOLUME_GB=${EMR_VOLUME_GB} 가 AMI 스냅샷 ${SNAP_GB}GB 보다 작습니다"; exit 1
fi
echo "  루트 장치 $ROOT_DEV / ${EMR_VOLUME_GB}GB gp3 ${EMR_VOLUME_THROUGHPUT}MB/s"

cat > /tmp/emr-lt-data.json <<JSON
{
  "ImageId": "${EMR_AMI_ID}",
  "BlockDeviceMappings": [
    {
      "DeviceName": "${ROOT_DEV}",
      "Ebs": {
        "VolumeSize": ${EMR_VOLUME_GB},
        "VolumeType": "gp3",
        "Throughput": ${EMR_VOLUME_THROUGHPUT},
        "DeleteOnTermination": true
      }
    }
  ],
  "SecurityGroupIds": ["${EMR_SG_ID}"],
  "IamInstanceProfile": {"Name": "${PROFILE_NAME}"},
  "UserData": "${USERDATA_B64}",
  "MetadataOptions": {
    "HttpTokens": "required",
    "HttpPutResponseHopLimit": 2
  },
  "TagSpecifications": [
    {"ResourceType": "instance", "Tags": [{"Key": "Project", "Value": "emr"}]},
    {"ResourceType": "volume",   "Tags": [{"Key": "Project", "Value": "emr"}]}
  ]
}
JSON

# MetadataOptions 두 줄을 꼭 읽어보자. 4단계에서 가장 조용하게 터지는 부분이다.
#
#   HttpTokens=required     : IMDSv1을 막는다. v1은 SSRF 한 방으로 인스턴스
#                             자격증명이 새는 경로라서 켤 이유가 없다.
#   HttpPutResponseHopLimit : 기본값 1이면 메타데이터 응답이 호스트를 한 번만
#                             거친다. 도커 브리지 네트워크는 그 한 홉을 이미
#                             써버려서 **컨테이너 안에서는 IMDS가 타임아웃난다.**
#                             리퍼가 자기 instance-id도, 스팟 회수 통지도 못 읽고
#                             조용히 아무 일도 안 하게 된다 — 에러도 안 난다.
#                             2로 올려야 컨테이너까지 닿는다.

echo "▶ 시작 템플릿 생성/갱신"
if aws ec2 describe-launch-templates --launch-template-names "$LT_NAME" >/dev/null 2>&1; then
  aws ec2 create-launch-template-version \
    --launch-template-name "$LT_NAME" \
    --source-version '$Latest' \
    --launch-template-data file:///tmp/emr-lt-data.json \
    --query 'LaunchTemplateVersion.VersionNumber' --output text
  aws ec2 modify-launch-template --launch-template-name "$LT_NAME" --default-version '$Latest' >/dev/null
  echo "  새 버전을 기본으로 지정했습니다"
else
  aws ec2 create-launch-template \
    --launch-template-name "$LT_NAME" \
    --launch-template-data file:///tmp/emr-lt-data.json \
    --query 'LaunchTemplate.LaunchTemplateId' --output text
fi

echo "✔ 시작 템플릿 준비 완료: $LT_NAME"
