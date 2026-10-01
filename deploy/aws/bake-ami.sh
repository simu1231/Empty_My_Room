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
echo "▶ 저장소 $REPO_ROOT / 루트 $EMR_ROOT / 태그 $EMR_IMAGE_TAG"

# ── 0. 경로 점검 ─────────────────────────────────────────────────────────
# 컨테이너는 호스트와 **같은 절대경로**로 모델을 마운트한다. 이 인스턴스의 실제
# 배치가 config.sh의 EMR_ROOT와 어긋나면 컨테이너는 멀쩡히 뜬 뒤 첫 요청에서
# import 에러로 죽는다 — 그때는 이미 AMI를 구운 뒤다. 굽기 전에 깨뜨린다.
[ "$REPO_ROOT" = "$EMR_REPO_DIR" ] || {
  echo "✗ 저장소는 $REPO_ROOT 에 있는데 config.sh는 $EMR_REPO_DIR 를 가리킨다"
  echo "  저장소를 옮기거나 EMR_ROOT 를 맞추세요."; exit 1; }

for d in miniconda3 sam-3d-objects uLayout omni3d detectron2 \
         pytorch3d_omni3d_build .cache/huggingface; do
  [ -d "$EMR_ROOT/$d" ] || { echo "✗ $EMR_ROOT/$d 가 없다 — 설치가 덜 끝났다"; exit 1; }
done

# 컨테이너는 uid 1000으로 돈다(호스트 파일을 uid로 매칭한다). HF 캐시는 락 파일을
# 쓰므로 읽기만으로는 부족하다. root 소유로 깔아두면 첫 추론에서 PermissionError다.
_owner=$(stat -c %u "$EMR_ROOT/.cache/huggingface")
[ "$_owner" = "1000" ] || {
  echo "✗ $EMR_ROOT/.cache/huggingface 소유자가 uid $_owner (uid 1000이어야 한다)"
  echo "  sudo chown -R 1000:1000 $EMR_ROOT"; exit 1; }
unset _owner

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
# 가디언의 상태 디렉터리를 여기서 만들지 않는다. 이젠 /run/emr (tmpfs) 이라
# 부팅마다 사라지고, guardian.sh 가 돌 때마다 직접 mkdir 한다.

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
sudo rm -f /var/log/emr-userdata.log
# 예전에는 가디언 실패 카운터가 /var/lib/emr 에 있었고, 여기서 그걸 지우고
# 있었다. 그런데 이 줄 **뒤에** smoke-test.sh 가 돌면서 스택을 내리면 가디언이
# 곧바로 다시 쌓기 시작해서, 스냅샷에는 결국 카운터가 박혔다. 지우는 순서를
# 바꾸는 대신 카운터를 tmpfs 로 옮겨서 고쳤다(guardian.sh 주석 참고).
# 아래는 옛 버전 AMI를 다시 굽는 경우를 위한 뒷정리다.
sudo rm -rf /var/lib/emr

echo "✔ 준비 완료 — 굽기 전에 두 가지를 순서대로 돌린다:"
echo "    ./smoke-test.sh    실제로 도는지 (세 모델에 진짜 작업을 통과시킨다)"
echo "    ./verify-ami.sh    있어야 할 것이 있는지"
echo "  둘 다 통과하면 스냅샷을 찍는다."
