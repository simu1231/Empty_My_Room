#!/bin/bash
# 스냅샷 찍기 **직전에 인스턴스 안에서** 돌린다. 여기서 막지 못한 문제는
# 전부 "부팅은 됐는데 요금만 나가는" 형태로 나타난다 — 제일 찾기 어렵고
# 제일 비싼 실패다.
#
# 특히 aws CLI. 없으면 가디언이 인스턴스를 회수할 수 없고(shutdown은 대안이
# 아니다 — ASG가 교체 인스턴스를 띄운다), 그 사실은 요금 고지서로만 알게 된다.
set -uo pipefail
cd "$(dirname "$0")"
. ./config.sh

FAIL=0
ok()   { echo "  ✔ $*"; }
bad()  { echo "  ✗ $*"; FAIL=1; }
warn() { echo "  ! $*"; }

echo "▶ 요금을 끊는 데 반드시 필요한 것"
command -v aws >/dev/null 2>&1 \
  && ok "aws CLI $(aws --version 2>&1 | cut -d' ' -f1)" \
  || bad "aws CLI 없음 — 가디언이 인스턴스를 회수할 수 없다"
# /opt/emr/bin 은 755라 일반 사용자로도 확인된다. sudo를 쓰면 비밀번호가
# 필요할 때 명령이 실패하고, 그 실패가 "파일 없음"으로 잘못 읽힌다.
[ -x /opt/emr/bin/self-retire.sh ] \
  && ok "self-retire.sh 설치됨" || bad "/opt/emr/bin/self-retire.sh 없음"
[ -x /opt/emr/bin/guardian.sh ] \
  && ok "guardian.sh 설치됨" || bad "/opt/emr/bin/guardian.sh 없음"
if systemctl is-enabled emr-guardian.timer >/dev/null 2>&1; then
  ok "emr-guardian.timer enabled ($(systemctl is-active emr-guardian.timer))"
else
  bad "emr-guardian.timer 가 enabled 가 아니다 — 재부팅하면 가디언이 안 돈다"
fi

echo "▶ 런타임"
command -v docker >/dev/null 2>&1 && ok "docker $(docker --version | cut -d' ' -f3 | tr -d ,)" \
  || bad "docker 없음"
systemctl is-enabled docker >/dev/null 2>&1 && ok "docker enabled" \
  || bad "docker 가 부팅 시 자동 시작이 아니다"
if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
  ok "nvidia 런타임 등록됨"
else
  bad "nvidia 런타임이 없다 — GPU 컨테이너가 안 뜬다"
fi
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null \
  | sed 's/^/  ✔ GPU /' || bad "nvidia-smi 실패"

echo "▶ 이미지 (태그 $EMR_IMAGE_TAG)"
for img in "emr/api:$EMR_IMAGE_TAG" "emr/worker:$EMR_IMAGE_TAG" "emr/gpu:$EMR_IMAGE_TAG"; do
  if sz=$(docker image inspect "$img" --format '{{.Size}}' 2>/dev/null); then
    ok "$img ($(( sz / 1024 / 1024 )) MB)"
  else
    bad "$img 없음 — bake-ami.sh 를 먼저 돌려야 한다"
  fi
done
# 태그가 서로 같은 이미지를 가리키면 CPU/GPU 빌드가 충돌한 것이다.
W=$(docker image inspect "emr/worker:$EMR_IMAGE_TAG" --format '{{.Id}}' 2>/dev/null || echo w)
G=$(docker image inspect "emr/gpu:$EMR_IMAGE_TAG"    --format '{{.Id}}' 2>/dev/null || echo g)
[ "$W" = "$G" ] && bad "emr/worker 와 emr/gpu 가 같은 이미지다 — 태그가 덮어써졌다" \
                || ok "worker / gpu 이미지가 서로 다르다"

echo "▶ 저장소와 가중치"
[ -d "$EMR_REPO_DIR" ] && ok "$EMR_REPO_DIR 존재" \
  || bad "$EMR_REPO_DIR 없음 — userdata가 cd 하다 죽는다"
for f in docker-compose.yml docker-compose.gpu.yml docker-compose.aws.yml; do
  [ -f "$EMR_REPO_DIR/deploy/$f" ] && ok "deploy/$f" || bad "deploy/$f 없음"
done
for d in $EMR_WARM_DIRS; do
  if [ -d "$d" ]; then ok "$(basename "$d") $(du -sh "$d" 2>/dev/null | cut -f1)"
  else bad "$d 없음 — 모델 가중치가 AMI에 안 들어갔다"; fi
done

echo "▶ 위생"
[ -f "$EMR_REPO_DIR/deploy/.env" ] \
  && warn ".env 가 남아있다 — userdata가 덮어쓰지만 AMI 공유 시 새어나간다" \
  || ok ".env 없음(부팅 때 생성된다)"
# sudo -n 을 쓴다. 비밀번호 프롬프트로 sudo가 실패하면 test 도 실패하는데,
# 그걸 "파일 없음"으로 읽으면 확인하지 못한 것을 통과시킨다.
for p in /root/.aws /home/ubuntu/.aws; do
  if ! sudo -n true 2>/dev/null; then
    warn "$p — sudo 불가라 확인하지 못했다(직접 봐야 한다)"
  elif sudo -n test -e "$p"; then
    bad "$p 가 AMI에 남아있다 — 인스턴스 역할을 쓰므로 있어선 안 된다"
  else
    ok "$p 없음"
  fi
done
df -h / | awk 'NR==2 {printf "  ✔ 루트 %s 중 %s 사용 (여유 %s)\n", $2, $3, $4}'

echo
if [ "$FAIL" -eq 0 ]; then
  echo "✔ 검증 통과 — 스냅샷을 찍어도 된다."
  echo "  aws ec2 create-image --instance-id <이 인스턴스> --name emr-gpu-$EMR_IMAGE_TAG --no-reboot"
else
  echo "✗ 검증 실패 — 이대로 구우면 부팅은 되고 요금만 나가는 인스턴스가 된다."
fi
exit "$FAIL"
