#!/usr/bin/env bash
# 단계 6 — 프런트를 S3 에 올리고 CloudFront 하나로 묶는다.
#
# 왜 CloudFront 를 끼우나
# ───────────────────────
# 프런트와 API 를 **같은 출처**로 만들기 위해서다. 지금은 브라우저가 S3 와
# EC2 를 각각 부르므로 CORS 가 필요하고, 프런트 빌드에 API 주소를 박아야 한다.
# CloudFront 뒤에 둘 다 넣으면 /api/* 만 API 로 보내면 되고, 프런트는 상대경로
# 하나로 끝난다 — jobs.js 의 JOB_API_BASE 기본값이 '' 인 것이 그 설계다.
# 덤으로 HTTPS 가 공짜로 생긴다. EC2 에 인증서를 넣을 필요가 없다.
#
# 순서가 왜 이런가
# ────────────────
#   버킷 → OAC → EIP → 배포 → 버킷정책 → 업로드
# 배포를 만들 때 **API 오리진 도메인이 이미 확정**돼 있어야 하므로 EIP 가 먼저다.
# 버킷 정책은 거꾸로 **배포 ARN** 이 있어야 쓸 수 있으므로 배포 뒤다.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
. ./config.sh

DRY_RUN=${DRY_RUN:-0}
say(){ echo "  $*"; }
run(){ if [ "$DRY_RUN" != "0" ]; then echo "    [계획] $*"; else "$@"; fi; }

# run() 은 출력을 버리므로 **ID 를 받아야 하는 생성 호출에는 쓸 수 없다**.
# 그걸 모르고 OAC 생성과 EIP 할당을 맨 aws 로 두었더니, DRY_RUN=1 로 "계획만
# 보려던" 실행이 실제로 리소스를 만들었다. 계획 모드에서는 가짜 ID 를 돌려주는
# 전용 래퍼를 둔다. 가짜 값은 한눈에 알아보게 DRYRUN- 접두사를 붙인다.
run_id(){ local fake="$1"; shift
  if [ "$DRY_RUN" != "0" ]; then echo "    [계획] $*" >&2; echo "$fake"; else "$@"; fi; }

: "${EMR_SITE_BUCKET:?EMR_SITE_BUCKET 이 비었다 — config.sh 경고를 확인하라}"
ACCT=$(aws sts get-caller-identity --query Account --output text)
API_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=$API_NAME" Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[0].InstanceId' --output text)
[ -n "$API_ID" ] && [ "$API_ID" != "None" ] || { echo "실행 중인 $API_NAME 인스턴스가 없다"; exit 1; }
say "API 인스턴스: $API_ID"

# ── 1. 정적 사이트 버킷 ─────────────────────────────────────────────────
# 퍼블릭 액세스는 네 스위치를 **전부** 끈다. OAC 를 쓰므로 버킷이 공개될
# 이유가 없고, 실수로 공개 ACL 이 붙는 경로도 같이 막힌다.
echo "[1/6] 사이트 버킷 $EMR_SITE_BUCKET"
if aws s3api head-bucket --bucket "$EMR_SITE_BUCKET" 2>/dev/null; then
  say "(이미 있음)"
else
  run aws s3api create-bucket --bucket "$EMR_SITE_BUCKET" \
    --region "$AWS_REGION" --create-bucket-configuration "LocationConstraint=$AWS_REGION"
  say "생성됨"
fi
run aws s3api put-public-access-block --bucket "$EMR_SITE_BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
run aws s3api put-bucket-tagging --bucket "$EMR_SITE_BUCKET" \
  --tagging "TagSet=[{Key=Project,Value=${PROJECT:-emr}}]"

# ── 2. OAC ──────────────────────────────────────────────────────────────
# OAC 자체는 아무 권한도 주지 않는다. "CloudFront 가 S3 를 부를 때 SigV4 로
# 서명해라"는 지시일 뿐이고, 실제 허용은 6단계의 버킷 정책이 한다.
echo "[2/6] OAC $EMR_CF_OAC_NAME"
OAC_ID=$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='$EMR_CF_OAC_NAME'].Id | [0]" --output text 2>/dev/null)
if [ -n "$OAC_ID" ] && [ "$OAC_ID" != "None" ]; then
  say "(이미 있음) $OAC_ID"
else
  OAC_ID=$(run_id "DRYRUN-OAC" aws cloudfront create-origin-access-control \
    --origin-access-control-config \
    "Name=$EMR_CF_OAC_NAME,Description=emr static site,SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=s3" \
    --query 'OriginAccessControl.Id' --output text)
  say "생성됨 $OAC_ID"
fi

# ── 3. EIP ──────────────────────────────────────────────────────────────
# **할당과 연결을 한 블록에 묶는다.** 할당만 하고 연결에 실패하면 미사용 EIP 로
# 월 $3.65 가 샌다. 연결이 실패하면 그 자리에서 release 한다.
echo "[3/6] API 고정 주소"
ALLOC=$(aws ec2 describe-addresses --filters "Name=instance-id,Values=$API_ID" \
  --query 'Addresses[0].AllocationId' --output text 2>/dev/null)
if [ -n "$ALLOC" ] && [ "$ALLOC" != "None" ]; then
  say "(이미 붙어 있음) $ALLOC"
else
  ALLOC=$(run_id "DRYRUN-EIP" aws ec2 allocate-address --domain vpc \
    --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Project,Value=${PROJECT:-emr}},{Key=Name,Value=$API_NAME}]" \
    --query AllocationId --output text)
  say "할당됨 $ALLOC"
  if [ "$DRY_RUN" != "0" ]; then
    say "[계획] associate-address --instance-id $API_ID --allocation-id $ALLOC"
  elif ! aws ec2 associate-address --instance-id "$API_ID" --allocation-id "$ALLOC" >/dev/null; then
    echo "  연결 실패 — 미사용 EIP 과금을 막기 위해 즉시 반납한다" >&2
    aws ec2 release-address --allocation-id "$ALLOC" || true
    exit 1
  fi
  say "연결됨 → $API_ID"
fi
API_DNS=$(aws ec2 describe-instances --instance-ids "$API_ID" \
  --query 'Reservations[].Instances[].PublicDnsName' --output text)
say "오리진 도메인: $API_DNS"

# ── 4. 배포 ─────────────────────────────────────────────────────────────
# 커스텀 에러는 503 하나만 다룬다. ErrorCachingMinTTL=0 이 핵심이다 —
# GPU 가 0대일 때 API 가 돌려주는 503(백프레셔)을 CloudFront 가 캐시해 버리면,
# 워커가 떠서 정상이 된 뒤에도 사용자는 한동안 503 을 계속 받는다.
#
# 403/404 → index.html 리라이트는 **넣지 않는다**. 이 프런트는 react-router 가
# 없어 딥링크가 없고, 커스텀 에러는 비헤이비어별이 아니라 **배포 전체**에
# 적용되므로 넣으면 /api/* 의 403 까지 index.html + 200 으로 둔갑한다.
echo "[4/6] CloudFront 배포"
DIST_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='$EMR_CF_COMMENT'].Id | [0]" --output text 2>/dev/null)
if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then
  say "(이미 있음) $DIST_ID"
else
  cat > /tmp/emr-cf-dist.json <<JSON
{
  "CallerReference": "$EMR_CF_COMMENT-$(date +%s)",
  "Comment": "$EMR_CF_COMMENT",
  "Enabled": true,
  "DefaultRootObject": "index.html",
  "PriceClass": "PriceClass_200",
  "Origins": {
    "Quantity": 2,
    "Items": [
      {
        "Id": "s3-site",
        "DomainName": "$EMR_SITE_BUCKET.s3.$AWS_REGION.amazonaws.com",
        "OriginAccessControlId": "$OAC_ID",
        "S3OriginConfig": { "OriginAccessIdentity": "" }
      },
      {
        "Id": "api-origin",
        "DomainName": "$API_DNS",
        "CustomOriginConfig": {
          "HTTPPort": $API_PORT,
          "HTTPSPort": 443,
          "OriginProtocolPolicy": "http-only",
          "OriginSslProtocols": { "Quantity": 1, "Items": ["TLSv1.2"] },
          "OriginReadTimeout": 60,
          "OriginKeepaliveTimeout": 5
        }
      }
    ]
  },
  "DefaultCacheBehavior": {
    "TargetOriginId": "s3-site",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": { "Quantity": 2, "Items": ["GET","HEAD"],
      "CachedMethods": { "Quantity": 2, "Items": ["GET","HEAD"] } },
    "CachePolicyId": "$EMR_CF_CACHE_OPTIMIZED",
    "Compress": true
  },
  "CacheBehaviors": {
    "Quantity": 1,
    "Items": [
      {
        "PathPattern": "/api/*",
        "TargetOriginId": "api-origin",
        "ViewerProtocolPolicy": "redirect-to-https",
        "AllowedMethods": { "Quantity": 7,
          "Items": ["GET","HEAD","OPTIONS","PUT","POST","PATCH","DELETE"],
          "CachedMethods": { "Quantity": 2, "Items": ["GET","HEAD"] } },
        "CachePolicyId": "$EMR_CF_CACHE_DISABLED",
        "OriginRequestPolicyId": "$EMR_CF_ORP_ALLVIEWER_NOHOST",
        "Compress": true
      }
    ]
  },
  "CustomErrorResponses": {
    "Quantity": 1,
    "Items": [
      { "ErrorCode": 503, "ErrorCachingMinTTL": 0 }
    ]
  }
}
JSON
  # 축약 문법(Key=Value,...)으로는 못 넘긴다. Origins.Items 처럼 **리스트 안에
  # 중첩된 객체**가 들어가면 파서가 따옴표에서 깨진다. --cli-input-json 으로
  # 통째로 넘긴다 — 그러려면 DistributionConfigWithTags 로 한 겹 감싸야 한다.
  # 태그 값은 환경으로 넘긴다. 여기에 "emr" 을 적어 두면 PROJECT 와 조용히
  # 어긋날 수 있는데, IAM 의 aws:RequestTag/Project 조건이 정확히 이 값을
  # 요구하므로 어긋나는 순간 AccessDenied 로 떨어진다.
  EMR_TAG_PROJECT="${PROJECT:-emr}" python3 - <<'WRAP'
import json, os
cfg = json.load(open("/tmp/emr-cf-dist.json"))
json.dump({"DistributionConfigWithTags": {
    "DistributionConfig": cfg,
    "Tags": {"Items": [{"Key": "Project", "Value": os.environ["EMR_TAG_PROJECT"]}]},
}}, open("/tmp/emr-cf-input.json", "w"))
WRAP
  DIST_ID=$(run_id "DRYRUN-DIST" aws cloudfront create-distribution-with-tags \
    --cli-input-json file:///tmp/emr-cf-input.json \
    --query 'Distribution.Id' --output text)
  say "생성됨 $DIST_ID"
fi
if [ "$DRY_RUN" != "0" ] && [ "$DIST_ID" = "DRYRUN-DIST" ]; then
  CF_DOMAIN="dryrun.cloudfront.net"
else
  CF_DOMAIN=$(aws cloudfront get-distribution --id "$DIST_ID" \
    --query 'Distribution.DomainName' --output text)
fi
say "주소: https://$CF_DOMAIN"

# ── 5. 버킷 정책 ────────────────────────────────────────────────────────
# **이 배포만** 읽을 수 있게 한다. AWS:SourceArn 조건이 없으면 다른 계정의
# CloudFront 배포도 이 버킷을 읽을 수 있다(혼동된 대리인 문제).
echo "[5/6] 버킷 정책"
cat > /tmp/emr-site-bucket-policy.json <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AllowThisDistributionOnly",
    "Effect": "Allow",
    "Principal": { "Service": "cloudfront.amazonaws.com" },
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::$EMR_SITE_BUCKET/*",
    "Condition": {
      "StringEquals": { "AWS:SourceArn": "arn:aws:cloudfront::$ACCT:distribution/$DIST_ID" }
    }
  }]
}
JSON
run aws s3api put-bucket-policy --bucket "$EMR_SITE_BUCKET" \
  --policy file:///tmp/emr-site-bucket-policy.json
say "적용됨"

# ── 6. 업로드 ───────────────────────────────────────────────────────────
# index.html 만 no-cache 로 따로 올린다. 해시 붙은 assets/ 는 영구 캐시해도
# 안전하지만, index.html 이 캐시되면 새 번들을 가리키지 못한다.
echo "[6/6] dist 업로드"
DIST_DIR="$(cd ../.. && pwd)/sam3d/frontend/dist"
[ -d "$DIST_DIR" ] || { echo "빌드 결과가 없다: $DIST_DIR (npm run build 먼저)"; exit 1; }
run aws s3 sync "$DIST_DIR" "s3://$EMR_SITE_BUCKET/" --delete \
  --exclude index.html --cache-control "public,max-age=31536000,immutable"
run aws s3 cp "$DIST_DIR/index.html" "s3://$EMR_SITE_BUCKET/index.html" \
  --cache-control "no-cache"

echo
echo "완료"
echo "  사이트    https://$CF_DOMAIN"
echo "  배포 ID   $DIST_ID"
echo "  버킷      $EMR_SITE_BUCKET"
echo "  API 오리진 $API_DNS:$API_PORT"
echo
echo "config.sh 또는 환경에 넣어두면 재실행이 빨라진다:"
echo "  export EMR_CF_DIST_ID=$DIST_ID"
