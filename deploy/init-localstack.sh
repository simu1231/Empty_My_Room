#!/bin/bash
# LocalStack이 준비되면 자동 실행된다(ready.d 훅).
# 여기서 만드는 리소스 3종은 실제 AWS에서도 그대로 필요하다.
# 5단계 Terraform이 이 스크립트와 똑같은 구성을 만들게 된다.
set -e

# 리전을 반드시 명시한다. awslocal의 기본값은 us-east-1이라, 이걸 빼면
# 큐가 us-east-1에 만들어지고 앱(ap-northeast-2)은 영영 못 찾는다.
# SQS/DynamoDB는 리전별로 완전히 격리되어 있어서 실제 AWS에서도 같은 실수가 난다.
export AWS_DEFAULT_REGION=ap-northeast-2

echo "▶ S3 버킷 생성"
awslocal s3 mb s3://emr-jobs

# 버킷 CORS. 브라우저는 결과 메쉬를 presigned URL로 S3에서 직접 받는데,
# 이 설정이 없으면 S3가 Access-Control-Allow-Origin을 안 내려주고 브라우저가
# 응답을 버린다. curl로 테스트하면 200이 나와서 정상처럼 보이는 게 함정이다.
# 운영에서는 AllowedOrigins를 실제 도메인으로 좁힌다.
awslocal s3api put-bucket-cors --bucket emr-jobs --cors-configuration '{
  "CORSRules": [{
    "AllowedOrigins": ["*"],
    "AllowedMethods": ["GET", "PUT"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }]
}'

echo "▶ 작업 큐 + DLQ 생성"
# DLQ가 없으면 실패한 메시지가 무한 재시도되며 GPU 비용을 계속 태운다.
# 큐마다 DLQ를 따로 둔다 — 하나를 같이 쓰면 실패한 메시지를 보고 어느
# 파이프라인이 터진 건지 알 수 없다. aws/15-resources.sh 와 같은 구성이다.
#
# VisibilityTimeout=120 : SAM3D가 50초 걸리므로 기본 30초로는 부족하다.
#                         워커가 하트비트로 더 연장하지만 시작값도 넉넉히 둔다.
# maxReceiveCount=3     : 3번 실패하면 DLQ로 보낸다.
for Q in emr-sam3d emr-scene; do
  DLQ_URL=$(awslocal sqs create-queue --queue-name "${Q}-dlq" --output text --query QueueUrl)
  DLQ_ARN=$(awslocal sqs get-queue-attributes --queue-url "$DLQ_URL" \
              --attribute-names QueueArn --output text --query 'Attributes.QueueArn')
  awslocal sqs create-queue --queue-name "$Q" --attributes "{
    \"VisibilityTimeout\": \"120\",
    \"MessageRetentionPeriod\": \"3600\",
    \"RedrivePolicy\": \"{\\\"deadLetterTargetArn\\\":\\\"$DLQ_ARN\\\",\\\"maxReceiveCount\\\":\\\"3\\\"}\"
  }"
done

echo "▶ DynamoDB 테이블 생성"
# PAY_PER_REQUEST: 유휴 시 비용이 0에 수렴한다. 스케일투제로와 궁합이 맞다.
awslocal dynamodb create-table \
  --table-name emr-jobs \
  --attribute-definitions AttributeName=job_id,AttributeType=S \
  --key-schema AttributeName=job_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST

awslocal dynamodb update-time-to-live \
  --table-name emr-jobs \
  --time-to-live-specification "Enabled=true,AttributeName=expires_at" || true

echo "✔ 초기화 완료"
