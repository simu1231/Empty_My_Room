#!/bin/bash
# bake-ami.sh 와 verify-ami.sh 사이에서, **스냅샷 찍기 전에** 돌린다.
#
# verify-ami.sh 는 "있어야 할 것이 있는지"를 본다. 이 스크립트는 "실제로 도는지"를
# 본다 — 다른 질문이다. 가중치가 제자리에 있고 경로도 맞는데 첫 추론에서 죽는
# 경우가 실제로 있었다(sam3d_runner.CKPT_DIR 에 개인 홈 경로가 박혀 있던 건).
# 워커는 작업을 받을 때까지 모델을 로드하지 않으므로, 한 번이라도 **진짜 작업을
# 통과시켜 보지 않으면** 그 종류의 실패는 운영에서 요금과 함께 발견된다.
#
# 여기서 실패하면 공짜로 고친다. 구운 뒤에 발견하면 재빌드 + 재굽기 +
# 시작 템플릿 갱신 + ASG 인스턴스 교체다.
set -uo pipefail
cd "$(dirname "$0")"
. ./config.sh

CF="-f docker-compose.yml -f docker-compose.gpu.yml"
DEPLOY="$EMR_REPO_DIR/deploy"
PYBIN="$EMR_ROOT/miniconda3/envs/sam3d/bin/python"

echo "▶ 태그 $EMR_IMAGE_TAG / 루트 $EMR_ROOT"

# compose 는 EMR_IMAGE_TAG 와 EMR_ROOT 를 ${VAR:-기본값} 으로 읽는다. config.sh 를
# source 하지 않고 돌리면 둘 다 기본값으로 떨어지는데, 그게 조용히 틀린다:
#   - 태그가 'dev' 가 되어 구워 둔 이미지를 못 찾고 **처음부터 다시 빌드**한다.
#   - EMR_ROOT 가 $HOME(/home/ubuntu)이 되어 없는 경로를 마운트하는데, 도커는
#     에러 대신 빈 디렉터리를 만들어 준다 → 스택은 뜨고 첫 요청에서 죽는다.
# 그래서 여기서 한 번 더 못을 박는다.
[ -n "${EMR_IMAGE_TAG:-}" ] || { echo "✗ EMR_IMAGE_TAG 가 비었다"; exit 1; }
[ "$EMR_IMAGE_TAG" = "dev" ] && { echo "✗ 태그가 dev 다 — git 저장소 밖에서 돌렸다"; exit 1; }
for img in "emr/api:$EMR_IMAGE_TAG" "emr/worker:$EMR_IMAGE_TAG" "emr/gpu:$EMR_IMAGE_TAG"; do
  docker image inspect "$img" >/dev/null 2>&1 \
    || { echo "✗ $img 없음 — bake-ami.sh 를 먼저 돌린다"; exit 1; }
done
nvidia-smi >/dev/null 2>&1 || { echo "✗ nvidia-smi 실패 — GPU 없이는 의미가 없다"; exit 1; }

# 스냅샷에 컨테이너나 볼륨이 남으면 안 된다. 중간에 끊겨도 반드시 치우도록
# trap 으로 건다 — Ctrl-C 로 멈춘 뒤 그대로 구우면 LocalStack 과 emr-state
# 볼륨이 AMI 에 박혀서, 뜨는 인스턴스마다 옛 큐 상태를 들고 시작한다.
cleanup() {
  echo "▶ 정리"
  ( cd "$DEPLOY" && docker compose $CF down -v --remove-orphans >/dev/null 2>&1 )
  rm -f "$DEPLOY/.env"
}
trap cleanup EXIT

# --no-build: 구워 둔 이미지만 쓴다. 빌드가 여기서 일어나면 그건 태그가 어긋났다는
#   뜻이고, 조용히 수십 분을 태운 끝에 **검증한 것과 다른 이미지**를 굽게 된다.
# --wait: 사이드카 healthcheck 가 ulayout_loaded / omni3d_loaded 를 실제로 확인한다.
#   즉 이 줄이 통과하는 것만으로 uLayout 과 Omni3D 의 모델 로드가 증명된다.
echo "▶ 스택 기동 (모델 로드까지 기다린다, 몇 분)"
if ! ( cd "$DEPLOY" && docker compose $CF up -d --no-build --wait ); then
  echo "✗ 스택이 정상 상태로 뜨지 못했다"
  ( cd "$DEPLOY" && docker compose $CF ps; docker compose $CF logs --tail 40 )
  exit 1
fi
echo "  ✔ uLayout / Omni3D 모델 로드 완료 (healthcheck 통과)"

echo "▶ API 대기"
for i in $(seq 1 60); do
  curl -sf http://localhost:8000/health >/dev/null 2>&1 && break
  [ "$i" = 60 ] && { echo "✗ API 가 /health 에 응답하지 않는다"; exit 1; }
  sleep 2
done
echo "  ✔ API 응답"

# 여기부터가 핵심이다. 첫 sam3d_mesh 작업이 콜드 — 워커가 그때 처음
# load_pipeline() 을 부르고, CKPT_DIR 이 틀렸다면 바로 여기서 터진다.
echo "▶ 실제 추론 (sam3d 콜드/웜/fd + room_layout + omni3d)"
[ -x "$PYBIN" ] || { echo "✗ $PYBIN 없음"; exit 1; }
( cd "$EMR_REPO_DIR" && "$PYBIN" deploy/test_e2e.py )
RC=$?

echo
if [ "$RC" -eq 0 ]; then
  echo "✔ 세 모델 모두 실제 작업을 통과했다 — 이제 ./verify-ami.sh 를 돌린다."
else
  echo "✗ 추론 실패 — 이대로 구우면 첫 요청에서 죽는 AMI 가 된다."
  ( cd "$DEPLOY" && docker compose $CF logs --tail 60 worker-sam3d )
fi
exit "$RC"
