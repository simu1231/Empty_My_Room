#!/bin/bash
# 부팅마다 실행된다. 이미 구워진 스택을 올릴 뿐이다 — 모델 다운로드/빌드는 하지 않는다(AMI 에 있다).
# 실패하면 인스턴스를 스스로 회수한다(아래 트랩): 실패한 채 살아 있으면 리퍼가 없어 요금만 나간다.
# 이 파일은 렌더 후 16KB 를 넘으면 안 된다(주석도 센다). config.sh 의 같은 제목 주석을 읽을 것.
set -euxo pipefail
# 콘솔에도 흘린다: 워커는 SSH 가 없고 실패하면 트랩이 인스턴스를 회수해 로그 파일이 사라진다.
# 콘솔을 미리 검사하지 않는 이유: tee 가 대상 하나를 못 열면 set -e 아래 exec 이 기동을 죽인다.
CONSOLE=/dev/console
[ -w "$CONSOLE" ] 2>/dev/null || CONSOLE=/dev/null
exec > >(tee /var/log/emr-userdata.log "$CONSOLE" | logger -t emr-userdata) 2>&1

REPO=__REPO_DIR__
IMAGE_TAG=__IMAGE_TAG__
WARM_JOBS=__WARM_JOBS__
WARM_SKIP="__WARM_SKIP__"
WARM_BG_DIRS="__WARM_BG_DIRS__"
WARM_BG_TIMEOUT=__WARM_BG_TIMEOUT__
EMR_ROOT=__EMR_ROOT__
# 버킷/테이블은 .env 와 컴포즈 덮어쓰기가 같은 변수를 쓰게 쉘로 올린다(따로 적으면 어긋난다).
S3_BUCKET=__S3_BUCKET__
DDB_TABLE=__DDB_TABLE__
RETIRE=/opt/emr/bin/self-retire.sh

# ── 실패 트랩 ──
# 실패하면 요금을 끊는다(배포 실패가 요금 누수보다 낫다). EXIT 에 건다 — ERR 은 `exit 1` 에서 안 돈다.
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

# ── 저장소 ──
STEP="저장소 확인"
[ -d "$REPO" ] || { echo "[userdata] $REPO 가 없다 — AMI가 잘못됐다"; exit 1; }
cd "$REPO"

# ── 모델 루트 확인 ──
# 없으면 도커가 빈 디렉터리를 만들어 첫 요청에서 죽는다 — 여기서 먼저 깨뜨린다.
STEP="모델 루트 확인"
for d in "$EMR_ROOT/miniconda3" "$EMR_ROOT/sam-3d-objects" "$EMR_ROOT/uLayout" \
         "$EMR_ROOT/omni3d" "$EMR_ROOT/detectron2" "$EMR_ROOT/pytorch3d_omni3d_build" \
         "$EMR_ROOT/sam2_repo" "$EMR_ROOT/lama_repo" "$EMR_ROOT/lama_model" \
         "$EMR_ROOT/.cache/huggingface"; do
  [ -d "$d" ] || { echo "[userdata] $d 가 없다 — AMI에 모델이 안 들어갔다"; exit 1; }
done

# ── 이미지 확인 ──
# 이미지가 없으면 compose 가 부팅 중 빌드한다(수십 분) — 빌드 대신 실패시킨다.
STEP="이미지 태그 확인"
for img in "emr/api:$IMAGE_TAG" "emr/worker:$IMAGE_TAG" "emr/gpu:$IMAGE_TAG"; do
  docker image inspect "$img" >/dev/null 2>&1 || {
    echo "[userdata] 이미지 $img 가 AMI에 없다 — bake-ami.sh 태그와 시작 템플릿이 어긋났다"
    exit 1
  }
done

# ── 환경변수 주입 ──
STEP="환경변수 주입"
umask 077   # .env는 소유자만 읽는다
cat > deploy/.env <<ENV
AWS_ENDPOINT_URL=
AWS_REGION=__REGION__
S3_BUCKET=$S3_BUCKET
DDB_TABLE=$DDB_TABLE
Q_SAM3D=__Q_SAM3D__
Q_SCENE=__Q_SCENE__
IDLE_EXIT_SEC=__IDLE_EXIT_SEC__
EMR_IMAGE_TAG=$IMAGE_TAG
EMR_ROOT=$EMR_ROOT
EMR_SD_WARM=__SD_WARM__
EMR_SD_WARM_DROP=__SD_WARM_DROP__
DRY_RUN=0
ENV
umask 022

# fal 이면 SSM 에서 키를 읽는다. set +x 필수 — xtrace 가 값을 찍는다. 실패하면 set -e 로 중단.
if [ "__SAM3D_BACKEND__" = fal ]; then
  STEP="FAL_KEY 읽기"
  set +x
  { printf 'SAM3D_BACKEND=fal\nFAL_KEY='; aws ssm get-parameter --region __REGION__ --name __FAL_KEY_PARAM__ --with-decryption --query Parameter.Value --output text; } >> deploy/.env
  set -x
  grep -q '^FAL_KEY=.' deploy/.env
fi

# ── 가디언 유예 조정 ──
# bake-ami.sh 가 구운 값을 드롭인으로 덮는다(config.sh 만 고치면 구운 AMI 에 안 먹는다).
STEP="가디언 유예 조정"
mkdir -p /etc/systemd/system/emr-guardian.service.d
cat > /etc/systemd/system/emr-guardian.service.d/10-grace.conf <<CONF
[Service]
Environment=GUARDIAN_GRACE_SEC=__GUARDIAN_GRACE_SEC__
Environment=GUARDIAN_FAIL_MIN=__GUARDIAN_FAIL_MIN__
CONF
systemctl daemon-reload

# 스냅샷 워밍은 부팅 경로에서 뺐다(손해였다). 되살리려면 config.sh EMR_WARM_TIMEOUT 의 측정표부터.

# ── 기동 ──
# --no-build: 컴포즈가 빌드하려 들면 부팅이 몇십 분이 된다.
# 네 번째 -f: AMI 안의 컴포즈 파일은 굳어 있어 유저데이터로 덮는다(docker-compose.aws.yml, config.sh LOG_GROUP).
STEP="컴포즈 덮어쓰기"
cat > /run/emr-deploy-env.yml <<YML
x-log: &log
  driver: awslogs
  options:
    awslogs-region: "__REGION__"
    awslogs-group: "__LOG_GROUP__"
    awslogs-create-group: "true"
    mode: non-blocking
    max-buffer-size: 4m
    tag: "{{.Name}}/{{.ID}}"
services:
  worker-sam3d: {logging: *log, environment: {S3_BUCKET: "$S3_BUCKET", DDB_TABLE: "$DDB_TABLE"}}
  worker-scene: {logging: *log, environment: {S3_BUCKET: "$S3_BUCKET", DDB_TABLE: "$DDB_TABLE"}}
  reaper:       {logging: *log, environment: {S3_BUCKET: "$S3_BUCKET", DDB_TABLE: "$DDB_TABLE", EXPECT_WORKERS: "__EXPECT_WORKERS__"}}
  ulayout:      {logging: *log}
  omni3d:       {logging: *log}
  backend:
    logging: *log
    environment:
      EMR_BACKEND_STATE_FILE: "__BACKEND_STATE__"
      EMR_BACKEND_BUSY_SEC: "__BACKEND_BUSY_SEC__"
    healthcheck:
      test: ["CMD", "$EMR_ROOT/miniconda3/envs/sam3d/bin/python", "-c",
             "import urllib.request,json,sys; d=json.load(urllib.request.urlopen('http://localhost:8001/health',timeout=5)); sys.exit(0 if d.get('extract')=='loaded' else 1)"]
YML
DC="docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml"
DC="$DC -f deploy/docker-compose.aws.yml -f /run/emr-deploy-env.yml"

# 워밍 게이트 플래그 디렉터리(/run 은 tmpfs). 컴포즈가 :ro 로 붙이므로 기동 전에 만든다.
mkdir -p /run/emr

# ── 기동과 동시에 백그라운드 워밍 ──
# 컴포즈 기동 앞에 둔다(컨테이너가 뜨는 수 분 동안 디스크가 한가하다). systemd-run 으로 분리하고
# nice 10 + io idle 로 컴포즈에 양보한다. `&` 는 fd 를 붙들어 cloud-init 이 안 끝난다.
# 위 mkdir -p /run/emr 뒤여야 한다. 근거/측정표: config.sh 의 EMR_WARM_BG_DIRS.
STEP="백그라운드 워밍 예약"
if [ -n "$WARM_BG_DIRS" ] && command -v systemd-run >/dev/null 2>&1; then
  cat > /run/emr-warm-bg.sh <<'BGEOF'
#!/bin/sh
set -u
t0=$(date +%s); L=/run/emr-warmbg.list; : > "$L"
# 목록은 bake-ami.sh 2.5 가 미리 만든다(find 가 53초).
if [ -s /opt/emr/warmlist ]; then
  cp /opt/emr/warmlist "$L"
else
  # 목록이 없는 옛 AMI 용 폴백. 느리지만 틀리지는 않는다.
  echo "[warm-bg] 구워둔 목록이 없다 — find 로 만든다(느리다)"
  # if/else: `[ -d ] && find || echo` 는 find 오류 때 '없음'을 잘못 찍는다. 파일 경로도 있어 -e 필요.
  for d in $BG_DIRS; do
    if [ -d "$d" ]; then
      find -L "$d" -type f -size +8M ! -name '*.a' -print 2>/dev/null >> "$L"
    elif [ -e "$d" ]; then echo "$d" >> "$L"
    else echo "[warm-bg] $d 없음 — 건너뜀"
    fi
  done
fi
if [ -n "${BG_SKIP:-}" ]; then
  grep -Ev "$(printf '%s\n' $BG_SKIP | paste -sd'|' -)" "$L" > "$L.f" || true
  mv "$L.f" "$L"
fi
echo "[warm-bg] 목록 조각 $(wc -l < "$L")개, 작성 $(( $(date +%s) - t0 ))초"
# 읽은 양은 디스크에 직접 묻는다.
rd() { t=0; for f in /sys/block/*/stat; do
    case "$f" in */loop*|*/ram*|*/zram*) continue;; esac
    t=$(( t + $(awk '{print $3}' "$f") )); done; echo "$t"; }
s0=$(rd); t1=$(date +%s)
# 리퍼가 중간에 끊을 수 있으니 진행을 주기적으로 남긴다.
( while [ -e "$L" ]; do sleep 30
    echo "[warm-bg] 진행 $(( ($(rd) - s0) / 2048 ))MB, $(( $(date +%s) - t1 ))초"
  done ) &
# 한 줄은 "경로 [skip] [count]"(4MiB 블록). 큰 파일을 조각내야 -P 가 꽉 찬다.
xargs -d '\n' -P "$BG_JOBS" -n 1 \
  sh -c 'set -f; set -- $1; dd if="$1" of=/dev/null bs=4M skip="${2:-0}" ${3:+count=$3} status=none 2>/dev/null || true' _ < "$L" || true
rm -f "$L"   # 진행 표시 루프를 멈춘다
echo "[warm-bg] 끝 — $(( ($(rd) - s0) / 2048 ))MB, 읽기 $(( $(date +%s) - t1 ))초, 전체 $(( $(date +%s) - t0 ))초"
BGEOF
  chmod +x /run/emr-warm-bg.sh
  if systemd-run --unit=emr-warm-bg --collect --nice=10 \
       --property=IOSchedulingClass=idle \
       --property=RuntimeMaxSec="$WARM_BG_TIMEOUT" \
       --property=ExecStopPost="/usr/bin/touch /run/emr/warm-bg.done" \
       --property=StandardOutput=journal+console \
       --property=StandardError=journal+console \
       --setenv=BG_DIRS="$WARM_BG_DIRS" --setenv=BG_SKIP="$WARM_SKIP" \
       --setenv=BG_JOBS="$WARM_JOBS" \
       /run/emr-warm-bg.sh >/dev/null 2>&1; then
    echo "[warm-bg] 분리 기동 — 컴포즈 기동과 겹쳐 돈다 (동시 $WARM_JOBS, 상한 ${WARM_BG_TIMEOUT}초, 우선순위 낮춤)"
  else
    echo "[warm-bg] systemd-run 실패 — 첫 요청만 느려진다. 기동은 계속한다"
    touch /run/emr/warm-bg.done   # 안 돌 거면 게이트를 즉시 연다
  fi
else
  echo "[warm-bg] 대상이 없거나 systemd-run 이 없다 — 건너뜀"
  touch /run/emr/warm-bg.done
fi

STEP="컴포즈 기동"
$DC up -d --no-build

# ── 설정이 컨테이너까지 닿았는지 확인 ──
# 실제 컨테이너의 환경변수를 읽어 맞는지 본다(컴포즈 파일은 틀린 쪽이라 믿을 수 없다 — 404 사태).
STEP="설정 도달 확인"
CID=$($DC ps -q worker-sam3d 2>/dev/null | head -1)
EFF=$(docker inspect "$CID" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | sed -n 's/^S3_BUCKET=//p' | head -1)
if [ "$EFF" != "$S3_BUCKET" ]; then
  echo "[userdata] 워커가 보는 버킷이 '$EFF' 다 — 배포가 정한 값은 '$S3_BUCKET'"
  echo "[userdata] 이대로 두면 모든 작업이 404 로 죽는다. 기동을 중단한다."
  exit 1
fi
echo "[userdata] 버킷 확인 — 워커가 $EFF 를 본다"

# ── 리퍼 확인 ──
# 리퍼가 안 떴으면 요금을 끊을 주체가 없다. 아는 즉시 실패한다.
STEP="리퍼 확인"
for i in $(seq 30); do
  docker ps --filter name=reaper --filter status=running --format '{{.Names}}' | grep -q . && break
  [ "$i" -eq 30 ] && { echo "[userdata] 리퍼가 30초 안에 안 떴다"; exit 1; }
  sleep 1
done

