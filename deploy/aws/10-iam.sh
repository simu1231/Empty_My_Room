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
#
# Logs 는 6단계 부하 테스트에서 추가했다. 이 배포에는 기기 밖으로 로그가 나오는
# 경로가 하나도 없었다 — SSH 키 없음, 워커 SG 인바운드 0개, 콘솔은 64KB 상한에
# 커널 부팅 메시지로 가득 찬다. 그래서 "sam3d 첫 건이 1230초"라는 현상을 보고도
# 컨테이너 안에서 무슨 일이 있었는지 끝까지 확인할 수 없었다.
#
# 순서가 중요하다: 이 권한을 **먼저** 넣고 나서 시작 템플릿에 awslogs 드라이버를
# 붙여야 한다. 로그 드라이버가 자격증명 때문에 실패하면 도커는 컨테이너를
# 시작하지 않는다 — 권한 없이 드라이버를 켜면 워커가 전부 못 뜬다.
#
# 범위는 /emr/ 접두어로만 좁힌다. CreateLogGroup 까지 주는 건
# awslogs-create-group=true 의 폴백용이다(평소에는 15-resources.sh 가 보존기간
# 7일로 미리 만들어 둔다 — 자동 생성된 그룹은 보존기간이 무한이라 계속 과금된다).
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
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream", "logs:PutLogEvents",
        "logs:DescribeLogStreams", "logs:CreateLogGroup"
      ],
      "Resource": [
        "arn:aws:logs:${AWS_REGION}:${ACCOUNT}:log-group:/emr/*",
        "arn:aws:logs:${AWS_REGION}:${ACCOUNT}:log-group:/emr/*:log-stream:*"
      ]
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
      "Sid": "FindGpuWorker",
      "Effect": "Allow",
      "Action": "ec2:DescribeInstances",
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
#
# FindGpuWorker 의 Resource:"*" 는 넓어 보이지만 좁힐 방법이 없다.
# ec2:DescribeInstances 는 **자원 수준 권한도 태그 조건 키도 지원하지 않는다**
# (AWS 서비스 권한 참조의 Describe* 계열 공통 제약이다). Condition 으로
# Project=emr 을 걸면 평가할 대상이 없어서 **모든 호출이 거부된다** — 좁히려다
# 기능이 통째로 죽는다.
#
# 그래서 좁히기는 애플리케이션 쪽(gpuproxy.py)에서 한다. 거기서 ASG 이름과
# Project 태그를 **둘 다** 본다. 읽기 전용이고 RunInstances/TerminateInstances
# 는 여전히 없으므로, 최악의 경우 새는 건 '이 계정에 어떤 인스턴스가 있는지'
# 라는 메타데이터뿐이다.

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
