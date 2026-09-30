#!/bin/bash
# 시작 템플릿 — "인스턴스 한 대를 어떻게 만들 것인가"의 설계도.
#
# ASG가 인스턴스를 띄울 때마다 이걸 읽는다. 인스턴스 타입은 여기 적지 않는다.
# 타입은 ASG의 혼합 인스턴스 정책이 후보 목록에서 그때그때 고르기 때문이다.
set -euo pipefail
cd "$(dirname "$0")" && source ./config.sh

: "${EMR_AMI_ID:?AMI ID를 지정하세요: EMR_AMI_ID=ami-xxxx $0}"
: "${EMR_SG_ID:?보안그룹 ID를 지정하세요: EMR_SG_ID=sg-xxxx $0}"

echo "▶ 유저데이터 생성 (설정값을 치환해 굽는다)"
sed -e "s|__REGION__|${AWS_REGION}|g" \
    -e "s|__S3_BUCKET__|${S3_BUCKET}|g" \
    -e "s|__DDB_TABLE__|${DDB_TABLE}|g" \
    -e "s|__Q_SAM3D__|${Q_SAM3D}|g" \
    -e "s|__Q_SCENE__|${Q_SCENE}|g" \
    -e "s|__IDLE_EXIT_SEC__|${IDLE_EXIT_SEC:-120}|g" \
    -e "s|__REPO_DIR__|${EMR_REPO_DIR}|g" \
    -e "s|__IMAGE_TAG__|${EMR_IMAGE_TAG}|g" \
    -e "s|__WARM_DIRS__|${EMR_WARM_DIRS}|g" \
    -e "s|__WARM_TIMEOUT__|${EMR_WARM_TIMEOUT}|g" \
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
# EC2 유저데이터 한도는 16KB(base64 인코딩 후 기준). 한글 주석은 글자당 3바이트라
# 생각보다 빨리 찬다. 넘으면 시작 템플릿 생성이 실패하는 게 아니라 인스턴스가
# 잘린 스크립트로 부팅한다.
if [ "${#USERDATA_B64}" -gt 16384 ]; then
  echo "✗ 유저데이터 ${#USERDATA_B64} 바이트 — 16384 한도 초과"
  exit 1
fi
echo "  유저데이터 ${#USERDATA_B64}/16384 바이트, 태그 ${EMR_IMAGE_TAG}"

cat > /tmp/emr-lt-data.json <<JSON
{
  "ImageId": "${EMR_AMI_ID}",
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
