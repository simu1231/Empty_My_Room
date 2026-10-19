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

# ── GPU 호환 사전 점검 ───────────────────────────────────────────────────
# 타입을 바꾸면 GPU 세대가 바뀐다. 직접 빌드한 CUDA 확장(omni3d 의 detectron2/pytorch3d)이 이 GPU 에서
# 도는지 스택을 올리기 전에 작은 텐서로 확인한다(몇 초). 실패하면 몇 분짜리 스택 기동을 건너뛴다.
# 통과해도 모델 전체가 정상이라는 뜻은 아니다 — 아래 실제 작업이 그걸 본다. EMR_GPU_COMPAT=0 으로 끈다.
if [ "${EMR_GPU_COMPAT:-1}" = 1 ]; then
  echo "▶ GPU 호환 사전 점검"
  COMPAT_FAIL=0
  for e in sam3d omni3d uLayout; do
    "$EMR_ROOT/miniconda3/envs/$e/bin/python" -W ignore ./check_gpu_compat.py --env "$e" || COMPAT_FAIL=1
  done
  [ "$COMPAT_FAIL" = 0 ] || { echo "✗ 이 GPU 에서 돌지 않는 CUDA 커널이 있다 — 이 인스턴스 타입은 쓸 수 없다"; exit 1; }
fi

# ── SAM3D 백엔드 ─────────────────────────────────────────────────────────
# AWS 배포는 fal 이 기본이다(config.sh EMR_SAM3D_BACKEND). 이 스모크가 로컬 SAM3D 추론을
# 무조건 요구하지 않는다 — fal 이면 fal 호출(요금이 든다)을, SAM3D_BACKEND=local 이면 예전처럼
# 로컬 GPU 추론을, E2E_SAM3D=skip 이면 SAM3D 를 건너뛰고 uLayout/Omni3D 만 검증한다.
# compose 는 셸 환경의 SAM3D_BACKEND / FAL_KEY 를 읽는다(.env 에 쓰지 않는다: 정리 단계가 지운다).
SAM3D_BACKEND=${SAM3D_BACKEND:-$EMR_SAM3D_BACKEND}
export SAM3D_BACKEND
E2E_MODE=${E2E_SAM3D:-$SAM3D_BACKEND}
echo "▶ SAM3D 백엔드=$SAM3D_BACKEND, SAM3D 검증=$E2E_MODE"
if [ "$E2E_MODE" = fal ] && [ "$SAM3D_BACKEND" != fal ]; then
  echo "✗ E2E_SAM3D=fal 인데 SAM3D_BACKEND=$SAM3D_BACKEND — 워커가 fal 로 안 뜬다"; exit 1
fi
if [ "$SAM3D_BACKEND" = fal ]; then
  if [ -z "${FAL_KEY:-}" ]; then
    # 키는 변수로만 흐른다(출력·set -x 없음). 인스턴스 역할에 ssm:GetParameter 권한이 필요하다.
    FAL_KEY=$(aws ssm get-parameter --region "$AWS_REGION" --name "$EMR_FAL_KEY_PARAM" \
                --with-decryption --query Parameter.Value --output text 2>/dev/null) \
      || { echo "✗ SSM $EMR_FAL_KEY_PARAM 를 못 읽었다 — 10-iam.sh(ReadFalKey) 적용 여부, 또는 FAL_KEY 를 환경변수로 줘라"; exit 1; }
    export FAL_KEY
  fi
  [ -n "$FAL_KEY" ] || { echo "✗ FAL_KEY 가 비었다"; exit 1; }
  [ "$E2E_MODE" = skip ] || echo "  ! fal 호출 ${E2E_SAM3D_RUNS:-1}회가 실행된다(요금). 건너뛰려면 E2E_SAM3D=skip"
fi

# ── 자원 측정(시스템 RAM · VRAM · 메모리 압력 · IOPS) ──────────────────────
# 통과 기준과 읽는 법은 deploy/aws/TRIAL_G6XL.md. EMR_MEASURE=0 으로 끈다.
SMOKE_DIR=$(pwd)   # 위에서 이 스크립트 디렉터리로 cd 했다(cleanup 이 상대경로로 못 찾는다)
MEASURE=${EMR_MEASURE:-1}
# EMR_MEASURE_ENFORCE: 측정 판정을 smoke 의 성공/실패에 **반영**할지.
#   0 = 측정은 보여 주기만 한다(기존 동작). 기준 미달이어도 종료 코드는 e2e 결과만 따른다.
#   1 = 측정 결과가 없거나(샘플 없음·측정기 오류·너무 짧음) 기준 미달이면 e2e 가 통과해도 종료 코드가 0 이 아니다
#       (3 = 기준 미달, 4 = 측정 없음/오류).
# 기본값: stable 프로파일은 0(기존과 같다), 시험 프로파일(trial-*)은 1. 시험 타입의 RAM/VRAM 이 목적이면 반드시 1 이어야
# 한다 — 그렇지 않으면 "작업은 됐지만 RAM 이 모자랐다"가 성공으로 보인다. 명시하면 그 값이 우선한다.
if [ -z "${EMR_MEASURE_ENFORCE+x}" ]; then
  if [ "$EMR_GPU_PROFILE" = stable ]; then EMR_MEASURE_ENFORCE=0; else EMR_MEASURE_ENFORCE=1; fi
fi
ENFORCE=$EMR_MEASURE_ENFORCE
case "$ENFORCE" in 0|1) ;; *) echo "✗ EMR_MEASURE_ENFORCE 는 0|1 이어야 한다: $ENFORCE"; exit 1 ;; esac
if [ "$ENFORCE" = 1 ] && [ "$MEASURE" != 1 ]; then
  echo "✗ EMR_MEASURE_ENFORCE=1 인데 EMR_MEASURE=0 이다 — 측정을 끄고 측정 결과를 강제할 수는 없다"; exit 1
fi
echo "▶ 자원 측정=$MEASURE, 판정 강제(ENFORCE)=$ENFORCE (프로파일 $EMR_GPU_PROFILE)"
E2E_RC=""   # e2e 단계 결과. 비어 있으면 e2e 에 도달하지 못했다
[ "$MEASURE" = 1 ] && bash ./measure-resources.sh start "smoke-${SAM3D_BACKEND}-$EMR_GPU_PROFILE"

# 스냅샷에 컨테이너나 볼륨이 남으면 안 된다. 중간에 끊겨도 반드시 치우도록
# trap 으로 건다 — Ctrl-C 로 멈춘 뒤 그대로 구우면 LocalStack 과 emr-state
# 볼륨이 AMI 에 박혀서, 뜨는 인스턴스마다 옛 큐 상태를 들고 시작한다.
cleanup() {
  local rc=$? mrc="" final
  trap - EXIT
  if [ "${MEASURE:-0}" = 1 ]; then
    echo "▶ 자원 측정 결과"
    bash "$SMOKE_DIR/measure-resources.sh" stop; mrc=$?
  fi
  echo "▶ 정리"
  ( cd "$DEPLOY" && docker compose $CF down -v --remove-orphans >/dev/null 2>&1 )
  rm -f "$DEPLOY/.env"

  # ── 최종 판정 ── 작업(e2e) 결과와 측정 결과를 **따로** 보이고, 강제 모드일 때만 측정을 종료 코드에 반영한다.
  final=$rc
  local e2e m
  if [ -z "$E2E_RC" ]; then e2e="실행되지 못함(중간에 중단: 종료 코드 $rc)"
  elif [ "$E2E_RC" -eq 0 ]; then e2e="통과"; else e2e="실패"; fi
  case "$mrc" in
    "") m="꺼짐(EMR_MEASURE=0)" ;;
    0)  m="기준 통과(초안 기준·이 측정 한 번에 한함)" ;;
    3)  m="기준 미달" ;;
    *)  m="측정 결과 없음/오류(코드 $mrc)" ;;
  esac
  if [ "$ENFORCE" = 1 ] && [ -n "$mrc" ] && [ "$mrc" -ne 0 ] && [ "$final" -eq 0 ]; then final=$mrc; fi
  echo
  echo "══════ 최종 판정 ══════"
  echo "  작업 검증(e2e)  : $e2e"
  echo "  자원 측정       : $m   [강제=$ENFORCE]"
  if [ "$final" -eq 0 ]; then
    if [ -n "$mrc" ] && [ "$mrc" -ne 0 ]; then
      echo "  전체            : 성공 — 단 측정이 '$m' 인데 강제 모드가 아니라 종료 코드에 반영되지 않았다(EMR_MEASURE_ENFORCE=1 로 강제)"
    else
      echo "  전체            : 성공"
    fi
  else
    echo "  전체            : 실패 (종료 코드 $final)"
  fi
  exit "$final"
}
trap cleanup EXIT

# --no-build: 구워 둔 이미지만 쓴다. 빌드가 여기서 일어나면 그건 태그가 어긋났다는
#   뜻이고, 조용히 수십 분을 태운 끝에 **검증한 것과 다른 이미지**를 굽게 된다.
# --wait: 사이드카 healthcheck 가 ulayout_loaded / omni3d_loaded 를 실제로 확인한다.
#   즉 이 줄이 통과하는 것만으로 uLayout 과 Omni3D 의 모델 로드가 증명된다.
echo "▶ 스택 기동 (모델 로드까지 기다린다, 몇 분)"
[ "$MEASURE" = 1 ] && bash ./measure-resources.sh mark "stack-up"
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
[ "$MEASURE" = 1 ] && bash ./measure-resources.sh mark "e2e"
( cd "$EMR_REPO_DIR" && E2E_SAM3D="$E2E_MODE" "$PYBIN" deploy/test_e2e.py )
RC=$?

# fal 모드의 워커는 SAM3D 로컬 파이프라인(13GB)을 올리면 안 된다. 올렸다면 fal 로 안 돈 것이다.
if [ "$SAM3D_BACKEND" = fal ]; then
  WLOG=$( cd "$DEPLOY" && docker compose $CF logs worker-sam3d 2>/dev/null )
  echo "$WLOG" | grep -m1 "SAM3D_BACKEND=" || echo "  ! 워커 시작 로그에 SAM3D_BACKEND 줄이 없다"
  if echo "$WLOG" | grep -q "SAM3D 파이프라인 로드"; then
    echo "✗ fal 모드인데 워커가 로컬 SAM3D 파이프라인을 로드했다"; RC=1
  else
    echo "  ✔ 로컬 SAM3D 파이프라인 로드 없음"
  fi
  if [ "$E2E_MODE" != skip ] && ! echo "$WLOG" | grep -q "\[fal-diag\]"; then
    echo "✗ [fal-diag] 로그가 없다 — fal 호출이 실제로 일어나지 않았다"; RC=1
  fi
fi

echo
E2E_RC=$RC
if [ "$RC" -eq 0 ]; then
  echo "✔ 작업 검증(e2e)만 통과했다 (SAM3D=$E2E_MODE). 측정 판정과 전체 결과는 아래 '최종 판정'을 본다."
else
  echo "✗ 추론 실패 — 이대로 구우면 첫 요청에서 죽는 AMI 가 된다."
  ( cd "$DEPLOY" && docker compose $CF logs --tail 60 worker-sam3d )
fi
exit "$RC"
