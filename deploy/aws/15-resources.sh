#!/bin/bash
# 큐 / 버킷 / 테이블을 실제 AWS에 만든다. 지금까지는 LocalStack 안에만 있었다
# (deploy/init-localstack.sh). 10-iam.sh 가 만든 정책이 이 리소스들의 ARN을
# 가리키므로, 이름이 바뀌면 10-iam.sh 를 다시 돌려야 한다.
#
# 여러 번 돌려도 안전하다(이미 있으면 설정만 맞춘다).
set -euo pipefail
cd "$(dirname "$0")" && source ./config.sh

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
TAGS="Key=Project,Value=emr"

# ── SQS ─────────────────────────────────────────────────────────────────
# 큐마다 DLQ를 따로 둔다. 로컬 초기화 스크립트는 둘이 DLQ 하나를 같이 썼는데,
# 그러면 실패한 메시지를 보고 어느 파이프라인이 터진 건지 알 수 없다.
#
# VisibilityTimeout=120 : SAM3D가 50초 걸린다. 기본 30초면 아직 처리 중인
#                         메시지가 다시 보이게 되고, 두 번째 워커가 같은 작업을
#                         또 집어간다(= GPU 요금 두 배). 워커가 하트비트로 더
#                         연장하지만 시작값도 넉넉해야 한다.
# maxReceiveCount=3     : 3번 실패하면 DLQ. 없으면 실패한 메시지가 무한
#                         재시도되며 GPU를 계속 깨운다.
echo "▶ SQS"
for Q in "$Q_SAM3D" "$Q_SCENE"; do
  DLQ="${Q}-dlq"
  # DLQ는 오래 보관한다. 실패를 나중에 들여다볼 수 있어야 의미가 있다.
  aws sqs create-queue --queue-name "$DLQ" \
    --attributes "MessageRetentionPeriod=1209600" \
    --tags "Project=emr" >/dev/null
  DLQ_URL=$(aws sqs get-queue-url --queue-name "$DLQ" --query QueueUrl --output text)
  DLQ_ARN=$(aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
              --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)

  aws sqs create-queue --queue-name "$Q" --tags "Project=emr" >/dev/null
  Q_URL=$(aws sqs get-queue-url --queue-name "$Q" --query QueueUrl --output text)
  # create-queue 는 이미 있는 큐의 속성을 안 바꾼다. set-queue-attributes 로 따로 맞춘다.
  aws sqs set-queue-attributes --queue-url "$Q_URL" --attributes "{
    \"VisibilityTimeout\": \"120\",
    \"MessageRetentionPeriod\": \"3600\",
    \"RedrivePolicy\": \"{\\\"deadLetterTargetArn\\\":\\\"$DLQ_ARN\\\",\\\"maxReceiveCount\\\":\\\"3\\\"}\"
  }"
  echo "  $Q → DLQ $DLQ"
done

# ── S3 ──────────────────────────────────────────────────────────────────
echo "▶ S3 $S3_BUCKET"
if aws s3api head-bucket --bucket "$S3_BUCKET" 2>/dev/null; then
  echo "  (이미 있음)"
else
  # ap-northeast-2 는 LocationConstraint 를 명시해야 한다. 빼면 us-east-1에
  # 만들어지고, 앱은 ap-northeast-2 를 보므로 영영 못 찾는다.
  aws s3api create-bucket --bucket "$S3_BUCKET" \
    --create-bucket-configuration "LocationConstraint=$AWS_REGION" >/dev/null
  echo "  생성됨"
fi

# 퍼블릭 차단. 업로드 사진과 결과 메쉬가 들어가는 버킷이라 기본값에 맡기지 않는다.
aws s3api put-public-access-block --bucket "$S3_BUCKET" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# CORS. 브라우저가 presigned URL 로 S3에서 직접 받는데, 이게 없으면 S3가
# Access-Control-Allow-Origin 을 안 내려주고 브라우저가 응답을 버린다.
# curl 로 테스트하면 200이 나와서 정상처럼 보이는 게 함정이다.
# TODO: 도메인 확정되면 AllowedOrigins 를 거기로 좁힌다.
aws s3api put-bucket-cors --bucket "$S3_BUCKET" --cors-configuration '{
  "CORSRules": [{
    "AllowedOrigins": ["*"],
    "AllowedMethods": ["GET", "PUT"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }]
}'

# 수명주기는 prefix 별로 나눈다. 예전엔 Prefix "" 에 7일 만료를 걸어서 버킷
# 전체를 대상으로 삼았는데, 같은 버킷 ami/ 아래에 AMI 부트스트랩 tar(hfcache 등
# 57GB)가 들어 있다. 그대로 두면 재굽기용 tar 가 7일 뒤 조용히 사라지고, 다음
# 부팅이 404 로 죽는다. 그래서 만료는 input/ 과 result/ 에만 건다.
#
# 세 번째 규칙이 따로 있는 이유: AbortIncompleteMultipartUpload 도 예전엔 같은
# Prefix "" 규칙에 얹혀 있었다. 만료만 좁히고 이걸 안 빼내면, 29.8GB tar 처럼
# 멀티파트로 올라가는 ami/ 업로드가 중간에 끊겼을 때 그 조각이 영구히 남아
# 요금을 낸다. 조각 정리는 버킷 전체에 유지하고, 만료만 분리한다.
aws s3api put-bucket-lifecycle-configuration --bucket "$S3_BUCKET" \
  --lifecycle-configuration '{
    "Rules": [
      {
        "ID": "expire-input",
        "Status": "Enabled",
        "Filter": {"Prefix": "input/"},
        "Expiration": {"Days": 7}
      },
      {
        "ID": "expire-result",
        "Status": "Enabled",
        "Filter": {"Prefix": "result/"},
        "Expiration": {"Days": 7}
      },
      {
        "ID": "abort-incomplete-uploads",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}
      }
    ]
  }'
aws s3api put-bucket-tagging --bucket "$S3_BUCKET" \
  --tagging 'TagSet=[{Key=Project,Value=emr}]'
echo "  퍼블릭 차단 / CORS / 수명주기 적용 (input,result 7일 / ami 만료없음)"

# ── DynamoDB ────────────────────────────────────────────────────────────
echo "▶ DynamoDB $DDB_TABLE"
if aws dynamodb describe-table --table-name "$DDB_TABLE" >/dev/null 2>&1; then
  echo "  (이미 있음)"
else
  # PAY_PER_REQUEST: 유휴 시 요금이 0에 수렴한다. 스케일투제로와 궁합이 맞다.
  # 프로비전드로 두면 GPU를 0으로 내려도 테이블 요금이 계속 나간다.
  aws dynamodb create-table --table-name "$DDB_TABLE" \
    --attribute-definitions AttributeName=job_id,AttributeType=S \
    --key-schema AttributeName=job_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --tags "$TAGS" >/dev/null
  aws dynamodb wait table-exists --table-name "$DDB_TABLE"
  echo "  생성됨"
fi
# TTL은 테이블이 ACTIVE 여야 걸린다. 이미 켜져 있으면 에러가 나므로 조회 후 처리.
TTL=$(aws dynamodb describe-time-to-live --table-name "$DDB_TABLE" \
        --query 'TimeToLiveDescription.TimeToLiveStatus' --output text)
if [ "$TTL" != "ENABLED" ] && [ "$TTL" != "ENABLING" ]; then
  aws dynamodb update-time-to-live --table-name "$DDB_TABLE" \
    --time-to-live-specification "Enabled=true,AttributeName=expires_at" >/dev/null
  echo "  TTL(expires_at) 켬"
else
  echo "  TTL 이미 $TTL"
fi

# ── CloudWatch Logs ─────────────────────────────────────────────────────
# 6단계 부하 테스트에서 추가했다. 그때까지 이 배포에는 컨테이너 로그가 기기 밖으로
# 나오는 경로가 아예 없었다 — 워커는 SSH 키가 없고(KeyName: None), 워커 보안그룹은
# 인바운드 규칙이 0개고, EC2 콘솔 출력은 64KB 상한이라 커널 부팅 메시지가 그걸 다
# 채운다. "sam3d 첫 건이 1230초"를 관측해 놓고도 컨테이너 안을 들여다볼 수 없었다.
#
# 그룹을 **미리** 만드는 이유는 보존기간이다. awslogs 드라이버가
# awslogs-create-group=true 로 자동 생성한 그룹은 보존기간이 "만료 없음"이고,
# 그러면 디버깅용 로그가 영구히 과금된다. 7일로 미리 박아두면 드라이버는 이미
# 있는 그룹을 그냥 쓴다(미리 만들기는 드라이버 쪽 자동 생성과 충돌하지 않는다).
#
# 그룹 하나에 서비스별 스트림을 둔다(유저데이터의 tag: "{{.Name}}/{{.ID}}").
# 그래야 `aws logs tail /emr/worker --follow` 한 줄로 다섯 컨테이너를 같이 본다 —
# 서비스마다 그룹을 쪼개면 사이드카와 워커의 시간 순서를 맞춰 볼 수 없다.
echo "▶ CloudWatch Logs $LOG_GROUP"
aws logs create-log-group --log-group-name "$LOG_GROUP" --tags Project=emr >/dev/null 2>&1 \
  || echo "  (이미 있음)"
aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 7
echo "  보존기간 7일"

# ── SNS (알림) ──────────────────────────────────────────────────────────
# DLQ 에 뭔가 쌓이면 메일을 받는다. create-topic 과 subscribe 는 둘 다 멱등이라
# 여러 번 돌려도 안전하다. 다만 이메일 구독은 **본인이 확인 메일을 눌러야**
# 활성화된다 — 안 누르면 PendingConfirmation 으로 남고 알림이 안 온다.
echo "▶ SNS 알림 토픽"
TOPIC_ARN=$(aws sns create-topic --name "$EMR_ALERT_TOPIC" --tags "$TAGS" \
              --query TopicArn --output text)
echo "  $TOPIC_ARN"
SUB=$(aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" \
        --query "Subscriptions[?Endpoint=='$EMR_ALERT_EMAIL'].SubscriptionArn | [0]" \
        --output text 2>/dev/null || echo None)
case "$SUB" in
  None|"")
    aws sns subscribe --topic-arn "$TOPIC_ARN" --protocol email \
      --notification-endpoint "$EMR_ALERT_EMAIL" >/dev/null
    echo "  구독 요청 → $EMR_ALERT_EMAIL"
    echo "  ⚠ 메일함의 'Confirm subscription' 을 눌러야 알림이 온다"
    ;;
  PendingConfirmation)
    echo "  ⚠ 구독 미승인 — $EMR_ALERT_EMAIL 의 확인 메일을 눌러라"
    ;;
  *)
    echo "  구독 확인됨 → $EMR_ALERT_EMAIL"
    ;;
esac

echo
echo "✔ 리소스 준비 완료"
echo "   큐     $Q_SAM3D / $Q_SCENE (+ 각각 -dlq)"
echo "   버킷   $S3_BUCKET"
echo "   테이블 $DDB_TABLE"
echo "   알림   $EMR_ALERT_TOPIC → $EMR_ALERT_EMAIL"
echo "   로그   $LOG_GROUP (보존 7일)  →  aws logs tail $LOG_GROUP --follow"
echo
echo "   버킷 이름이 바뀌었다면 ./10-iam.sh 를 다시 돌려야 정책 ARN이 맞는다."
