#!/bin/bash
# AMI를 굽기 위한 준비. **AMI 원본 인스턴스 안에서** 실행한다(로컬 PC 아님).
#
#   1) 도커 이미지를 git SHA 태그로 빌드해 둔다
#   2) 가디언/자가회수 스크립트를 /opt/emr/bin 에 설치하고 systemd 타이머를 켠다
#   3) 스냅샷에 남으면 안 되는 것들을 지운다
#
# 왜 이미지를 미리 굽나 — `docker compose up -d` 는 이미지가 없으면 그 자리에서
# 빌드한다. 부팅 중 빌드는 수십 분이고 그동안 GPU 인스턴스 요금이 계속 나간다.
# 왜 가디언을 여기서 설치하나 — userdata가 설치하면, userdata가 실패했을 때
# 가디언도 없다. 가디언은 userdata 실패를 잡으라고 있는 물건이라 앞뒤가 안 맞는다.
set -euo pipefail
cd "$(dirname "$0")"
. ./config.sh

REPO_ROOT=$(cd ../.. && pwd)
echo "▶ 저장소 $REPO_ROOT / 태그 $EMR_IMAGE_TAG"

# ── 1. 이미지 빌드 ───────────────────────────────────────────────────────
# 컴포즈 파일에 image: 태그를 박아뒀으므로 build 만으로 태그가 붙는다.
# CPU용(emr/worker)과 GPU용(emr/gpu)이 같은 디렉터리를 쓰지만 태그가 달라서
# 서로 덮어쓰지 않는다 — 예전엔 gpu 오버라이드가 build만 바꾸고 image를 물려받아
# CPU 이미지를 GPU 이미지로 덮어쓸 수 있는 상태였다.
export EMR_IMAGE_TAG
( cd "$REPO_ROOT/deploy" && \
  docker compose -f docker-compose.yml -f docker-compose.gpu.yml build )

# ── 2. 호스트 스크립트 설치 ──────────────────────────────────────────────
sudo install -d -m 755 /opt/emr/bin
sudo install -m 755 self-retire.sh /opt/emr/bin/self-retire.sh
sudo install -m 755 guardian.sh    /opt/emr/bin/guardian.sh
sudo install -d -m 755 /var/lib/emr

sudo tee /etc/systemd/system/emr-guardian.service >/dev/null <<UNIT
[Unit]
Description=EMR 가디언 — 리퍼가 죽었을 때 인스턴스를 회수해 요금을 끊는다
After=docker.service

[Service]
Type=oneshot
Environment=GUARDIAN_GRACE_SEC=${GUARDIAN_GRACE_SEC}
Environment=GUARDIAN_FAIL_MIN=${GUARDIAN_FAIL_MIN}
ExecStart=/opt/emr/bin/guardian.sh
UNIT

# OnBootSec 를 짧게 둬도 스크립트 안에서 uptime 유예를 보므로 안전하다.
# Persistent=false: 꺼져 있던 동안의 실행을 몰아서 하지 않는다.
sudo tee /etc/systemd/system/emr-guardian.timer >/dev/null <<'UNIT'
[Unit]
Description=EMR 가디언 1분 주기

[Timer]
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=10s
Persistent=false

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable emr-guardian.timer
sudo systemctl start  emr-guardian.timer

# ── 3. 스냅샷 위생 ───────────────────────────────────────────────────────
# .env 는 인스턴스가 뜰 때 userdata가 다시 만든다. AMI에 남겨두면 옛 큐 이름이
# 굳어버리고, 혹시 자격증명이 들어가면 AMI를 공유하는 순간 같이 나간다.
sudo rm -f "$REPO_ROOT/deploy/.env"
# 인스턴스 역할을 쓰므로 정적 키가 AMI에 있을 이유가 없다.
sudo rm -rf /root/.aws /home/ubuntu/.aws
# cloud-init 상태를 지워야 새 인스턴스에서 userdata가 처음처럼 돈다.
sudo cloud-init clean --logs 2>/dev/null || true
sudo rm -f /var/log/emr-userdata.log /var/lib/emr/guardian.fail

echo "✔ 준비 완료 — 이제 ./verify-ami.sh 를 돌리고, 통과하면 스냅샷을 찍는다."
