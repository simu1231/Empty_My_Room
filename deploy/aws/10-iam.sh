#!/bin/bash
# GPU 워커 인스턴스가 쓸 IAM 역할을 만든다.
#
# 왜 역할(role)인가? 액세스 키를 AMI나 유저데이터에 박으면, 인스턴스가 뜨고
# 죽기를 반복하는 동안 그 키가 계속 복제된다. 역할을 붙이면 EC2가 임시 자격증명을
# 메타데이터로 넣어주고 자동으로 갱신된다 — 키 파일이 아예 존재하지 않는다.
set -euo pipefail
cd "$(dirname "$0")" && source ./config.sh

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

echo "▶ 신뢰 정책: EC2만 이 역할을 맡을 수 있다"
aws iam create-role --role-name "$ROLE_NAME" \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ec2.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }' >/dev/null 2>&1 || echo "  (이미 있음)"

echo "▶ 권한 정책"
# 권한을 최소로 좁힌다. 특히 autoscaling:TerminateInstanceInAutoScalingGroup 은
# 워낙 강한 권한이라 Condition으로 "우리 두 ASG"에만 걸어둔다. 안 그러면 이 역할을
# 얻은 코드가 계정 안의 아무 ASG나 비울 수 있다.
cat > /tmp/emr-worker-policy.json <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Queue",
      "Effect": "Allow",
      "Action": [
        "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
        "sqs:GetQueueUrl", "sqs:GetQueueAttributes"
      ],
      "Resource": [
        "arn:aws:sqs:${AWS_REGION}:${ACCOUNT}:${Q_SAM3D}",
        "arn:aws:sqs:${AWS_REGION}:${ACCOUNT}:${Q_SCENE}"
      ]
    },
    {
      "Sid": "Objects",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::${S3_BUCKET}/*"
    },
    {
      "Sid": "JobStatus",
      "Effect": "Allow",
      "Action": ["dynamodb:UpdateItem", "dynamodb:GetItem"],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT}:table/${DDB_TABLE}"
    },
    {
      "Sid": "ReaperReadsOwnGroup",
      "Effect": "Allow",
      "Action": "autoscaling:DescribeAutoScalingInstances",
      "Resource": "*"
    },
    {
      "Sid": "ReaperTerminatesSelfOnly",
      "Effect": "Allow",
      "Action": "autoscaling:TerminateInstanceInAutoScalingGroup",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "autoscaling:ResourceTag/Project": "emr"
        }
      }
    }
  ]
}
JSON

aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name "${ROLE_NAME}-policy" \
  --policy-document file:///tmp/emr-worker-policy.json

echo "▶ 인스턴스 프로파일 (EC2에 역할을 붙이는 껍데기)"
aws iam create-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null 2>&1 || echo "  (이미 있음)"
aws iam add-role-to-instance-profile \
  --instance-profile-name "$PROFILE_NAME" --role-name "$ROLE_NAME" 2>/dev/null || echo "  (이미 연결됨)"

# ── API 서버용 역할 ─────────────────────────────────────────────────────
# API 서버는 별도 인스턴스(t4g.small)에서 상시 가동이라 역할도 따로 둔다.
# GPU 워커 역할을 돌려쓰면 API가 인스턴스를 종료할 권한까지 갖게 된다.
API_ROLE=${PROJECT:-emr}-api-role
API_PROFILE=${PROJECT:-emr}-api-profile

echo "▶ API 서버 역할"
aws iam create-role --role-name "$API_ROLE" \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ec2.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }' >/dev/null 2>&1 || echo "  (이미 있음)"

cat > /tmp/emr-api-policy.json <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Enqueue",
      "Effect": "Allow",
      "Action": ["sqs:SendMessage", "sqs:GetQueueUrl"],
      "Resource": [
        "arn:aws:sqs:${AWS_REGION}:${ACCOUNT}:${Q_SAM3D}",
        "arn:aws:sqs:${AWS_REGION}:${ACCOUNT}:${Q_SCENE}"
      ]
    },
    {
      "Sid": "Objects",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::${S3_BUCKET}/*"
    },
    {
      "Sid": "JobStatus",
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT}:table/${DDB_TABLE}"
    },
    {
      "Sid": "WakeGpuWorkers",
      "Effect": "Allow",
      "Action": ["autoscaling:DescribeAutoScalingGroups"],
      "Resource": "*"
    },
    {
      "Sid": "SetCapacityOnSpotGroupOnly",
      "Effect": "Allow",
      "Action": "autoscaling:SetDesiredCapacity",
      "Resource": "*",
      "Condition": {"StringEquals": {"autoscaling:ResourceTag/Project": "emr"}}
    }
  ]
}
JSON
# API에 TerminateInstanceInAutoScalingGroup 을 주지 않는 게 요점이다.
# API는 용량을 '올리기만' 한다. 내리는 건 작업 중인지 아는 리퍼만 한다.

aws iam put-role-policy --role-name "$API_ROLE" \
  --policy-name "${API_ROLE}-policy" \
  --policy-document file:///tmp/emr-api-policy.json

aws iam create-instance-profile --instance-profile-name "$API_PROFILE" >/dev/null 2>&1 || echo "  (이미 있음)"
aws iam add-role-to-instance-profile \
  --instance-profile-name "$API_PROFILE" --role-name "$API_ROLE" 2>/dev/null || echo "  (이미 연결됨)"

# ── 배포 사용자 자신의 SNS 권한 ──────────────────────────────────────────
# 위의 둘은 '인스턴스가' 쓸 역할이고, 이건 '이 스크립트를 돌리는 사람'이
# 15-resources.sh 에서 알림 토픽을 만들 수 있게 하는 권한이다.
# emr-deploy 에는 SNS 권한이 아예 없어서 CreateTopic 이 AuthorizationError 로 막힌다.
# AmazonSNSFullAccess 를 붙이면 한 줄로 끝나지만 계정 안 모든 토픽이 열린다.
# 쓰는 토픽이 하나뿐이니 그 ARN 하나로 좁힌다.
CALLER_USER=$(aws sts get-caller-identity --query Arn --output text | sed -n 's|.*:user/||p')
if [ -z "$CALLER_USER" ]; then
  echo "▶ SNS 권한: 건너뜀 (IAM 사용자가 아니라 역할로 실행 중)"
else
  echo "▶ SNS 권한: $CALLER_USER → $EMR_ALERT_TOPIC"
  cat > /tmp/emr-deploy-sns.json <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "ManageAlertTopicOnly",
    "Effect": "Allow",
    "Action": [
      "sns:CreateTopic",
      "sns:TagResource",
      "sns:GetTopicAttributes",
      "sns:SetTopicAttributes",
      "sns:Subscribe",
      "sns:ListSubscriptionsByTopic"
    ],
    "Resource": "arn:aws:sns:${AWS_REGION}:${ACCOUNT}:${EMR_ALERT_TOPIC}"
  }]
}
JSON
  aws iam put-user-policy --user-name "$CALLER_USER" \
    --policy-name "emr-deploy-sns" \
    --policy-document file:///tmp/emr-deploy-sns.json
  echo "  인라인 정책 emr-deploy-sns 적용 (토픽 1개로 한정)"
fi

echo "✔ IAM 준비 완료"
echo "   GPU 워커: $PROFILE_NAME"
echo "   API 서버: $API_PROFILE"
echo "   알림 토픽: $EMR_ALERT_TOPIC (쓰기 권한만)"
