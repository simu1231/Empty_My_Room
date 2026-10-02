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
# /dev/console 에도 흘린다. 워커는 SSH 키도 인바운드 규칙도 없고(큐에서 당겨
# 쓰는 구조라 접속할 일이 없다) 실패하면 트랩이 인스턴스를 회수해서
# /var/log/emr-userdata.log 가 같이 사라진다. logger 만으로는 안 된다 —
# journald 기본값이 ForwardToConsole=no 이고 get-console-output 은 직렬
# 콘솔에 쓴 것만 보여준다(전부 기록되는데 밖에서만 안 보였다). 콘솔을 미리
# 검사하는 건 tee 가 대상 하나를 못 열면 1 을 돌려주고, set -e 아래의 exec
# 이라 기동이 통째로 죽기 때문이다.
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
# 버킷/테이블은 .env 와 아래 컴포즈 덮어쓰기가 **같은 변수**를 쓰게 쉘로 올린다.
# 두 군데에 따로 적으면 조용히 어긋난다 — 이미 가디언 유예와 워밍 상한에서
# 한 번 당했다.
S3_BUCKET=__S3_BUCKET__
DDB_TABLE=__DDB_TABLE__
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
S3_BUCKET=$S3_BUCKET
DDB_TABLE=$DDB_TABLE
Q_SAM3D=__Q_SAM3D__
Q_SCENE=__Q_SCENE__
IDLE_EXIT_SEC=__IDLE_EXIT_SEC__
EMR_IMAGE_TAG=$IMAGE_TAG
EMR_ROOT=$EMR_ROOT
DRY_RUN=0
ENV
umask 022

# ── 가디언 유예 조정 ────────────────────────────────────────────────────
# GUARDIAN_GRACE_SEC 은 bake-ami.sh 가 emr-guardian.service 에 **구워 넣는다.**
# 그래서 config.sh 만 고치면 이미 구운 AMI에는 안 먹는다(EMR_IMAGE_TAG 와 같은
# 함정이다). 유예가 부팅 시간보다 짧으면 가디언이 멀쩡히 부팅 중인 워커를
# 죽인다 — 2026-10-01 에 20.2분째 워밍 중이던 워커가 그렇게 회수됐다.
# 드롭인은 본체 뒤에 처리되므로 같은 변수는 이쪽 값이 이긴다.
STEP="가디언 유예 조정"
mkdir -p /etc/systemd/system/emr-guardian.service.d
cat > /etc/systemd/system/emr-guardian.service.d/10-grace.conf <<CONF
[Service]
Environment=GUARDIAN_GRACE_SEC=__GUARDIAN_GRACE_SEC__
Environment=GUARDIAN_FAIL_MIN=__GUARDIAN_FAIL_MIN__
CONF
systemctl daemon-reload

# 스냅샷 워밍은 여기(부팅 경로)에 있었는데 기동 뒤로 옮겼다. 맨 아래 참고.
# 네 번 재보니 부팅 경로에서 데우는 건 매번 손해였다(674 → 345초). 되살리고
# 싶어지면 먼저 config.sh 의 EMR_WARM_TIMEOUT 자리에 남긴 측정표를 읽을 것.

# ── 기동 ─────────────────────────────────────────────────────────────────
# --no-build 가 핵심이다. 위에서 태그를 확인했지만, 컴포즈가 만에 하나 다른
# 서비스를 빌드하려 들면 부팅이 몇십 분짜리가 된다. 빌드는 굽는 시점의 일이다.
#
# 네 번째 -f 가 하는 일: AMI 안의 컴포즈 파일은 **구운 시점에 굳는다.** 6단계
# 부하 테스트에서 기본 파일의 `S3_BUCKET: emr-jobs` 리터럴을 AWS 오버라이드가
# 안 덮는 걸 발견했고(실제 버킷엔 계정 해시가 붙는다), 워커 14건이 전부
# HeadObject 404 로 죽었다. 저장소는 고쳤지만 낡은 AMI 로도 떠야 한다.
# 유저데이터는 시작 템플릿에 있어 재굽기 없이 바꿀 수 있으니 여기서 마지막으로
# 덮는다. 값은 위 .env 와 같은 쉘 변수라 새로 적는 숫자는 없다.
STEP="컴포즈 덮어쓰기"
cat > /run/emr-deploy-env.yml <<YML
services:
  worker-sam3d: {environment: {S3_BUCKET: "$S3_BUCKET", DDB_TABLE: "$DDB_TABLE"}}
  worker-scene: {environment: {S3_BUCKET: "$S3_BUCKET", DDB_TABLE: "$DDB_TABLE"}}
  reaper:       {environment: {S3_BUCKET: "$S3_BUCKET", DDB_TABLE: "$DDB_TABLE"}}
YML
DC="docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml"
DC="$DC -f deploy/docker-compose.aws.yml -f /run/emr-deploy-env.yml"

# 워커의 워밍 게이트가 볼 플래그 디렉터리. /run 은 tmpfs라 부팅마다 비어
# 있다 — 지난 부팅의 플래그가 남으면 게이트가 처음부터 열려 의미가 없다.
# 컴포즈가 :ro 로 붙이므로 기동 **전에** 만든다.
mkdir -p /run/emr

STEP="컴포즈 기동"
$DC up -d --no-build

# ── 설정이 컨테이너까지 닿았는지 확인 ───────────────────────────────────
# 위 404 사태의 진짜 교훈은 "오버라이드를 빠뜨렸다"가 아니라 **빠뜨렸는지
# 아무도 안 본다**는 쪽이다. 컨테이너는 멀쩡히 떴고 리퍼도 떴고 가디언도
# 조용했다. 틀린 건 첫 작업이 들어온 뒤에야 드러났고, 그때는 이미 돈이 나갔다.
# 그래서 부팅에서 **실제 컨테이너의 환경변수**를 읽어 맞는지 본다.
# 컴포즈 파일을 읽어 확인하면 의미가 없다 — 틀린 건 바로 그 파일이었다.
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

# ── 리퍼 확인 ────────────────────────────────────────────────────────────
# 여기까지 성공해도 리퍼 컨테이너가 안 떴으면 요금을 끊을 주체가 없다.
# 가디언이 5분 뒤 잡긴 하지만, 아는 즉시 실패하는 편이 낫다.
STEP="리퍼 확인"
for i in $(seq 30); do
  docker ps --filter name=reaper --filter status=running --format '{{.Names}}' | grep -q . && break
  [ "$i" -eq 30 ] && { echo "[userdata] 리퍼가 30초 안에 안 떴다"; exit 1; }
  sleep 1
done

# ── 기동 뒤 백그라운드 워밍 ──────────────────────────────────────────────
# 부팅을 막지 않으면서 첫 요청(스모크 187초)만 데운다. 근거와 측정표는
# config.sh 의 EMR_WARM_BG_DIRS 주석에 있다. 여기서 중요한 건 셋뿐이다:
#   systemd-run : `&` 로 띄우면 상속한 fd 를 붙들어 cloud-init 이 끝난 것으로
#                 안 보이고, 가디언의 "running 이면 판단 보류"가 안 풀린다.
#   우선순위     : 실제 작업이 들어오면 양보해야 한다(nice 10 + io idle).
#   journal+console : 콘솔로 내보내야 바깥에서 측정할 수 있다.
STEP="백그라운드 워밍 예약"
if [ -n "$WARM_BG_DIRS" ] && command -v systemd-run >/dev/null 2>&1; then
  cat > /run/emr-warm-bg.sh <<'BGEOF'
#!/bin/sh
set -u
t0=$(date +%s); L=/run/emr-warmbg.list; : > "$L"
# 목록은 AMI 를 구울 때 미리 만든다(bake-ami.sh 2.5). AMI 안의 불변 데이터라
# 부팅마다 셀 이유가 없다 — find 가 혼자 53초였다(경위는 config.sh).
if [ -s /opt/emr/warmlist ]; then
  cp /opt/emr/warmlist "$L"
else
  # 목록이 없는 옛 AMI 용 폴백. 느리지만 틀리지는 않는다.
  echo "[warm-bg] 구워둔 목록이 없다 — find 로 만든다(느리다)"
  # if/else 로 쓴다. `[ -d ] && find || echo` 는 find 가 권한 오류 등으로 0이
  # 아닌 값을 내면 "없음"을 잘못 찍는다 — 틀린 로그가 제일 비싸다. BG_DIRS 는
  # 이제 파일 경로도 담으므로(-d 만 보면 전부 건너뛴다) -e 가지도 둔다.
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
# 작성 시간을 **따로** 찍는다. find 가 혼자 53초 먹는 걸 몰라서 처리량을 세 번
# 잘못 계산했다.
echo "[warm-bg] 목록 조각 $(wc -l < "$L")개, 작성 $(( $(date +%s) - t0 ))초"
# 읽은 양은 디스크에 직접 묻는다(끝낸 파일만 더하면 중간까지 읽힌 양이 빠진다).
rd() { t=0; for f in /sys/block/*/stat; do
    case "$f" in */loop*|*/ram*|*/zram*) continue;; esac
    t=$(( t + $(awk '{print $3}' "$f") )); done; echo "$t"; }
s0=$(rd); t1=$(date +%s)
# 리퍼가 유휴 회수로 중간에 끊을 수 있으니 진행을 주기적으로 남긴다. 끝 줄만
# 찍으면 "얼마나 데웠나"를 영원히 모른다. 서브셸의 sleep 이 고아로 남는 건
# 여기서는 걱정 없다 — systemd 유닛이라 cgroup 째로 정리된다.
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
    echo "[warm-bg] 분리 기동 — 아래 기동 완료 뒤에도 계속 돈다 (동시 $WARM_JOBS, 상한 ${WARM_BG_TIMEOUT}초, 우선순위 낮춤)"
  else
    echo "[warm-bg] systemd-run 실패 — 첫 요청만 느려진다. 기동은 계속한다"
    touch /run/emr/warm-bg.done   # 안 돌 거면 게이트를 즉시 연다
  fi
else
  echo "[warm-bg] 대상이 없거나 systemd-run 이 없다 — 건너뜀"
  touch /run/emr/warm-bg.done
fi
