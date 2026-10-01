#!/bin/bash
# 인스턴스가 뜰 때마다 실행된다. 하는 일은 하나 — 이미 구워진 스택을 올린다.
#
# 여기서 모델을 내려받거나 conda 환경을 설치하거나 도커 이미지를 빌드하지 않는다.
# 전부 AMI에 들어 있어야 한다(약 80GB). 부팅할 때마다 하면 콜드스타트가 수십 분이
# 되고, 그러면 스케일투제로로 아낀 돈을 대기 시간으로 도로 토해낸다.
#
# 이 스크립트가 실패하면 **인스턴스를 스스로 회수한다**(아래 트랩). 실패한 채
# 살아있는 인스턴스가 제일 비싸기 때문이다 — 리퍼가 안 떠서 아무도 회수하지 않고,
# ASG 헬스체크는 EC2 타입이라 "켜져 있음"만 보고 정상으로 판단한다. 그 상태로
# g6.xlarge는 일 없이 시간당 $0.45, 한 달 $327을 먹는다.
set -euxo pipefail
exec > >(tee /var/log/emr-userdata.log | logger -t emr-userdata) 2>&1

REPO=__REPO_DIR__
IMAGE_TAG=__IMAGE_TAG__
WARM_TIMEOUT=__WARM_TIMEOUT__
EMR_ROOT=__EMR_ROOT__
RETIRE=/opt/emr/bin/self-retire.sh

# ── 실패 트랩 ────────────────────────────────────────────────────────────
# 실패하면 조용히 남지 말고 요금을 끊는다. 배포가 실패하는 쪽이 요금이 새는
# 쪽보다 낫다 — 실패는 ASG 활동 로그와 /var/log/emr-userdata.log 에 남는다.
#
# ERR 이 아니라 EXIT 에 건다. ERR 트랩은 `exit 1` 로 일부러 죽는 자리(이미지
# 없음, 리퍼 안 뜸 …)에서는 안 돈다 — 정작 제일 회수해야 할 경우들이다.
STEP="시작"
on_exit() {
  local rc=$?
  trap - EXIT
  set +x
  [ "$rc" -eq 0 ] && { echo "[userdata] 기동 완료 tag=$IMAGE_TAG"; exit 0; }
  echo "[userdata] 실패 (단계=$STEP, 종료코드 $rc) — 인스턴스를 회수한다"
  if [ -x "$RETIRE" ]; then "$RETIRE" "userdata 실패: $STEP"
  else echo "[userdata] 치명적: $RETIRE 가 없다. 수동으로 종료해야 한다."; fi
  exit "$rc"
}
trap on_exit EXIT

# ── 저장소 ───────────────────────────────────────────────────────────────
# 경로는 시작 템플릿이 주입한다. 예전엔 개발 PC 홈(/home/<사용자>/...)이 박혀
# 있었는데, EC2 기본 사용자는 ubuntu라 그 경로가 없다 → cd 실패 → 위 트랩 직행.
STEP="저장소 확인"
[ -d "$REPO" ] || { echo "[userdata] $REPO 가 없다 — AMI가 잘못됐다"; exit 1; }
cd "$REPO"

# ── 모델 루트 확인 ───────────────────────────────────────────────────────
# compose가 이 경로들을 바인드 마운트한다. **없으면 도커가 조용히 빈 디렉터리를
# root 소유로 만들어 준다.** 컨테이너는 정상적으로 뜨고, 리퍼도 뜨고, 겉보기엔
# 다 성공한 뒤 첫 요청에서 ModuleNotFoundError로 죽는다. 여기서 먼저 깨뜨린다.
STEP="모델 루트 확인"
for d in "$EMR_ROOT/miniconda3" "$EMR_ROOT/sam-3d-objects" "$EMR_ROOT/uLayout" \
         "$EMR_ROOT/omni3d" "$EMR_ROOT/detectron2" "$EMR_ROOT/pytorch3d_omni3d_build" \
         "$EMR_ROOT/.cache/huggingface"; do
  [ -d "$d" ] || { echo "[userdata] $d 가 없다 — AMI에 모델이 안 들어갔다"; exit 1; }
done

# ── 이미지 확인 ──────────────────────────────────────────────────────────
# `docker compose up -d`는 이미지가 없으면 **그 자리에서 빌드한다**. 부팅 중
# 빌드는 수십 분이고, 그동안 요금은 계속 나간다. 그래서 먼저 태그가 실제로
# 있는지 보고, 없으면 빌드 대신 크게 실패한다(=회수된다).
STEP="이미지 태그 확인"
for img in "emr/api:$IMAGE_TAG" "emr/worker:$IMAGE_TAG" "emr/gpu:$IMAGE_TAG"; do
  docker image inspect "$img" >/dev/null 2>&1 || {
    echo "[userdata] 이미지 $img 가 AMI에 없다 — bake-ami.sh 태그와 시작 템플릿이 어긋났다"
    exit 1
  }
done

# ── 환경변수 주입 ────────────────────────────────────────────────────────
# 큐/버킷 이름은 환경변수로만 준다. 코드에는 기본값만 있고 어느 큐를 보는지는
# 배포가 정한다. 덕분에 나중에 sam3d와 scene을 다른 인스턴스로 쪼갤 때 코드를
# 한 줄도 안 고치고 컴포즈만 나누면 된다.
STEP="환경변수 주입"
umask 077   # .env는 소유자만 읽는다
cat > deploy/.env <<ENV
AWS_ENDPOINT_URL=
AWS_REGION=__REGION__
S3_BUCKET=__S3_BUCKET__
DDB_TABLE=__DDB_TABLE__
Q_SAM3D=__Q_SAM3D__
Q_SCENE=__Q_SCENE__
IDLE_EXIT_SEC=__IDLE_EXIT_SEC__
EMR_IMAGE_TAG=$IMAGE_TAG
EMR_ROOT=$EMR_ROOT
DRY_RUN=0
ENV
umask 022

# ── 스냅샷 워밍 ──────────────────────────────────────────────────────────
# EBS 스냅샷에서 복원한 볼륨은 블록을 처음 읽을 때 S3에서 끌어온다(지연 로딩).
# 가중치 31GB를 추론 중에 처음 읽으면 첫 작업이 몇 분 걸린다. 미리 훑어서 그
# 비용을 부팅 시간으로 옮긴다 — 어차피 기다릴 거면 사용자 요청 앞이 낫다.
#
# 장치명(/dev/nvme1n1)을 찍지 않는다. 장치 번호는 볼륨 구성에 따라 바뀌고,
# 틀리면 `|| true` 때문에 조용히 넘어가서 워밍이 아예 안 된 걸 모른다. 게다가
# fio의 --runtime 은 상한이라(둘 중 먼저 오는 쪽) 125MB/s gp3에서 180초면
# 80GB 중 22GB만 데운다. 파일 경로로 읽으면 장치와 무관하고, 필요한 가중치만
# 읽어서 볼륨 전체보다 빠르다.
warm() {
  local t0 n=0
  t0=$(date +%s)
  for d in __WARM_DIRS__; do
    [ -d "$d" ] || { echo "[warm] $d 없음 — 건너뜀"; continue; }
    # -size +8M: 작은 설정 파일은 어차피 금방 읽힌다. 큰 가중치만 데운다.
    while IFS= read -r -d '' f; do
      [ $(( $(date +%s) - t0 )) -lt "$WARM_TIMEOUT" ] || {
        echo "[warm] ${WARM_TIMEOUT}초 상한 도달 — 남은 파일은 첫 요청 때 읽힌다"; return 0; }
      dd if="$f" of=/dev/null bs=1M status=none 2>/dev/null || true
      n=$((n+1))
    done < <(find "$d" -type f -size +8M -print0 2>/dev/null)
  done
  echo "[warm] ${n}개 파일, $(( $(date +%s) - t0 ))초"
}
STEP="스냅샷 워밍"
warm   # 실패해도 기동은 계속한다(느려질 뿐이지 틀리지는 않는다)

# ── 기동 ─────────────────────────────────────────────────────────────────
# --no-build 가 핵심이다. 위에서 태그를 확인했지만, 컴포즈가 만에 하나 다른
# 서비스를 빌드하려 들면 부팅이 몇십 분짜리가 된다. 빌드는 굽는 시점의 일이다.
STEP="컴포즈 기동"
docker compose -f deploy/docker-compose.yml \
               -f deploy/docker-compose.gpu.yml \
               -f deploy/docker-compose.aws.yml up -d --no-build

# ── 리퍼 확인 ────────────────────────────────────────────────────────────
# 여기까지 성공해도 리퍼 컨테이너가 안 떴으면 요금을 끊을 주체가 없다.
# 가디언이 5분 뒤 잡긴 하지만, 아는 즉시 실패하는 편이 낫다.
STEP="리퍼 확인"
for i in $(seq 30); do
  docker ps --filter name=reaper --filter status=running --format '{{.Names}}' | grep -q . && break
  [ "$i" -eq 30 ] && { echo "[userdata] 리퍼가 30초 안에 안 떴다"; exit 1; }
  sleep 1
done
