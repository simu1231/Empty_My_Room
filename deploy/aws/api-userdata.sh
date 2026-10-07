#!/bin/bash
# API 서버(t4g.small, arm64)가 뜰 때마다 실행된다.
# 50-api.sh 가 밑줄 두 개로 감싼 자리표시자를 치환해서 올린다. 직접 돌리지 말 것.
#
# GPU 워커 유저데이터와 **일부러 반대로** 만든 곳이 두 군데다.
#
# 1. 여기서 빌드한다. 워커는 모델 80GB 때문에 AMI 를 굽지만 API 는 의존성이
#    4개뿐이라 1~2분이면 끝난다. AMI 를 굽고 스냅샷을 월 $5 내는 쪽이 더 비싸다.
# 2. **실패해도 인스턴스를 회수하지 않는다.** 워커는 실패한 채 살아 있는 게
#    제일 비싸서(시간당 $0.45) 트랩이 죽이지만, API 는 시간당 $0.02 고 SSH 로
#    들어가 고칠 수 있다. 여기서 자폭시키면 로그까지 같이 사라져서, 왜 안 떴는지
#    모르는 채로 재기동만 반복하게 된다.
set -euxo pipefail
CONSOLE=/dev/console
[ -w "$CONSOLE" ] 2>/dev/null || CONSOLE=/dev/null
exec > >(tee /var/log/emr-api-userdata.log "$CONSOLE" | logger -t emr-api) 2>&1

REPO_URL=__REPO_URL__
GIT_REF=__GIT_REF__
EMR_ROOT=__EMR_ROOT__
REPO=$EMR_ROOT/Empty_My_Room
PORT=__PORT__

# ── 스왑 ──
# 2GB 에서 docker build 중 pip 가 휠을 풀 때 순간적으로 몰린다. 휠만 쓰므로
# 컴파일은 없지만, OOM killer 가 빌드를 죽이면 원인이 로그에 안 남고
# "Killed" 한 줄만 뜬다. 1GB 면 충분하고 EBS 8GB 중 1GB 를 쓴다.
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=1024
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

# ── 패키지 ──
dnf install -y docker git
systemctl enable --now docker

# ── 저장소 ──
# 공개 저장소라 자격증명이 필요 없다. --depth 1 로 받는다 — 전체 이력은
# 350MB 가 넘고 여기서는 쓸 일이 없다.
mkdir -p "$EMR_ROOT"
rm -rf "$REPO"
git clone --depth 1 --branch "$GIT_REF" "$REPO_URL" "$REPO"
cd "$REPO"
TAG=$(git rev-parse --short HEAD)

# ── 환경변수 ──
# 컨테이너는 이 파일만 본다(systemd 유닛의 --env-file). 코드에는 기본값만
# 있고 어느 큐/버킷을 보는지는 배포가 정한다 — 워커와 같은 원칙이다.
#
# AWS_ENDPOINT_URL 을 빈 값으로 둬야 boto3 가 실제 AWS 를 본다
# (main.py 가 `os.getenv(...) or None` 로 처리한다). 지우면 안 된다 —
# 없으면 LocalStack 기본값이 살아날 자리가 생긴다.
#
# 자격증명은 쓰지 않는다. emr-api-profile 이 붙어 있어 IMDS 에서 온다.
# 키를 넣으면 오히려 역할을 가린다.
umask 077
cat > "$REPO/deploy/.env" <<ENV
AWS_ENDPOINT_URL=
AWS_PUBLIC_ENDPOINT_URL=
AWS_REGION=__REGION__
S3_BUCKET=__S3_BUCKET__
DDB_TABLE=__DDB_TABLE__
Q_SAM3D=__Q_SAM3D__
Q_SCENE=__Q_SCENE__
ASG_SPOT=__ASG_SPOT__
CORS_ORIGINS=__CORS_ORIGINS__
CAPACITY_NUDGE_INTERVAL=__NUDGE__
PROJECT=__PROJECT__
GPU_PORT=__GPU_PORT__
PREWARM_RATE=__PREWARM_RATE__
ENV
umask 022

# ── 빌드 ──
# 컨텍스트가 deploy/ 인 이유는 api/Dockerfile 머리말에 있다 —
# worker/jobspec.py 를 같이 넣어야 검증 규칙이 워커와 갈라지지 않는다.
cd "$REPO/deploy"
docker build -t "emr/api:$TAG" -t emr/api:current -f api/Dockerfile .

# ── 서비스 ──
# --restart 대신 systemd 로 묶는다. 재부팅·크래시 복구는 같지만, 상태를
# systemctl 로 볼 수 있고 로그가 journald 에 모인다.
cat > /etc/systemd/system/emr-api.service <<UNIT
[Unit]
Description=Empty My Room - Job API
After=docker.service
Requires=docker.service

[Service]
Restart=always
RestartSec=5
ExecStartPre=-/usr/bin/docker rm -f emr-api
ExecStart=/usr/bin/docker run --rm --name emr-api \\
  -p $PORT:8000 --env-file $REPO/deploy/.env emr/api:current
ExecStop=/usr/bin/docker stop emr-api

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now emr-api

# ── 확인 ──
# 여기서 실패해도 종료 코드만 남기고 인스턴스는 살려 둔다(머리말 2번).
set +e
for i in $(seq 1 30); do
  curl -sf "http://127.0.0.1:$PORT/health" && { echo "[emr-api] 기동 완료 tag=$TAG"; exit 0; }
  sleep 2
done
echo "[emr-api] /health 가 60초 안에 응답하지 않았다 — SSH 로 들어가서"
echo "          journalctl -u emr-api -n 100 과 /var/log/emr-api-userdata.log 를 볼 것"
exit 0
