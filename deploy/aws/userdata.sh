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
# /dev/console 에도 흘린다. 이게 없으면 이 스크립트가 실패했을 때 로그를 볼
# 방법이 없다. 워커는 SSH 키도 인바운드 규칙도 없고(그게 맞다 — 큐에서
# 당겨 쓰는 구조라 누구도 접속할 일이 없다), 실패하면 트랩이 인스턴스를
# 곧바로 회수해서 /var/log/emr-userdata.log 자체가 사라진다.
#
# logger 만으로는 안 된다. 그건 syslog/journald 로 가고, journald 는 기본값이
# ForwardToConsole=no 라 직렬 콘솔에 안 나온다. EC2 의 get-console-output 은
# 직렬 콘솔에 쓴 것만 보여준다 — 즉 지금까지는 전부 무언가에 기록되지만
# 정작 밖에서는 아무것도 안 보였다.
#
# 콘솔이 없거나 못 쓰는 환경도 있으므로 미리 검사한다. tee 는 대상 하나가
# 안 열리면 종료코드를 1로 돌려주는데, 이 줄은 set -e 아래의 exec 이라
# 거기서 기동이 통째로 죽는다.
CONSOLE=/dev/console
[ -w "$CONSOLE" ] 2>/dev/null || CONSOLE=/dev/null
exec > >(tee /var/log/emr-userdata.log "$CONSOLE" | logger -t emr-userdata) 2>&1

REPO=__REPO_DIR__
IMAGE_TAG=__IMAGE_TAG__
WARM_TIMEOUT=__WARM_TIMEOUT__
WARM_JOBS=__WARM_JOBS__
WARM_SKIP="__WARM_SKIP__"
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

# ── 스냅샷 워밍 ──────────────────────────────────────────────────────────
# 스냅샷에서 복원한 볼륨은 블록을 처음 읽을 때 S3에서 끌어온다(지연 로딩).
# 추론 중에 처음 읽으면 첫 작업이 몇 분 걸리므로 미리 훑어 부팅 시간으로 옮긴다.
# 장치명(/dev/nvme1n1)이 아니라 파일 경로로 읽는다 — 장치 번호는 구성 따라
# 바뀌고, 틀리면 조용히 건너뛰어 워밍이 0바이트인 걸 모른다.
#
# 반드시 병렬이어야 한다. 순차 dd 는 250 MB/s 볼륨에서 10.5 MB/s 밖에 못 냈고
# (2026-10-01 첫 실부팅) 그게 콜드스타트 21분의 원인이었다. 지연 로딩은
# 대역폭이 아니라 왕복 지연에 묶이기 때문이다. 자세한 근거와 EMR_WARM_SKIP
# 목록의 출처는 config.sh 의 EMR_WARM_JOBS / EMR_WARM_SKIP 주석에 있다.
#
# 상한은 xargs 전체가 아니라 dd 하나하나에 건다(근거는 아래). xargs 를 통째로
# 감싸면 자식 dd 가 고아로 남아 컴포즈 기동 중에도 디스크를 계속 두드린다.
# set +x: 파일이 수백 개라 추적을 켜 두면 콘솔 버퍼(64KB)가 앞부분을 밀어낸다.
warm() {
  local t0 raw=/run/emr-warmlist.raw list=/run/emr-warmlist done=/run/emr-warmdone
  local flag=/run/emr-warming deadline bytes files el mbps pat read_bytes mon
  local s0 disk_mb
  set +x
  # 읽은 양은 **디스크에게 직접 묻는다.** 다 읽은 파일만 더하면 상한에 걸렸을 때
  # 중간까지 읽힌 수십 GB가 통째로 빠진다 — 실제로 26GB를 읽고 16.6GB로 보고해
  # 속도를 39 MB/s 로 과소평가했다(CloudWatch 실측은 70 MB/s 였다). 그 숫자가
  # 다음 조정의 유일한 근거라 틀리면 엉뚱한 데를 고치게 된다.
  # /sys/block/<디스크>/stat 의 3번째 값이 누적 읽기 섹터(512B)다. 파티션은
  # /sys/block 에 없으므로 중복 집계되지 않는다.
  rdsect() { local t=0 f n
    for f in /sys/block/*/stat; do n=${f%/stat}; n=${n##*/}
      case "$n" in loop*|ram*|zram*) continue;; esac
      t=$(( t + $(awk '{print $3}' "$f") ))
    done; echo "$t"; }
  t0=$(date +%s)
  deadline=$(( t0 + WARM_TIMEOUT ))

  # +8M 만. '*.a' 제외 — 정적 라이브러리 5GB는 링크용이라 런타임에 안 읽힌다.
  : > "$raw"
  for d in __WARM_DIRS__; do
    if [ -d "$d" ]; then
      find "$d" -type f -size +8M ! -name '*.a' -printf '%s\t%p\n' 2>/dev/null >> "$raw"
    else
      echo "[warm] $d 없음 — 건너뜀"
    fi
  done

  # WARM_SKIP: 세 서비스가 안 읽는 가중치를 뺀다(config.sh 참고).
  if [ -n "$WARM_SKIP" ]; then
    pat=$(printf '%s\n' $WARM_SKIP | paste -sd'|' -)
    grep -Ev "$pat" "$raw" > "$list" || true
  else
    cp "$raw" "$list"
  fi

  files=$(wc -l < "$list")
  if [ "$files" -eq 0 ]; then
    echo "[warm] 대상이 없다 — 건너뜀 (EMR_WARM_DIRS 가 맞는지 확인할 것)"
    set -x
    return 0
  fi
  bytes=$(awk -F'\t' '{t+=$1} END{print t+0}' "$list")
  echo "[warm] 대상 ${files}개 $(( bytes / 1048576 ))MB (제외 뒤), 동시 ${WARM_JOBS}개, 상한 ${WARM_TIMEOUT}초"

  # 큰 것부터. 상한에 걸려도 무거운 가중치는 먼저 들어와 있게 된다.
  # 다 읽은 것만 $done 에 남긴다. 상한에 걸렸을 때 대상 크기로 속도를 계산하면
  # 안 읽은 양을 읽은 척하게 되는데, 이 숫자가 다음 조정의 유일한 근거다.
  #
  # dd 를 timeout 으로 감싼다. "시작 전에 마감을 본다"만으로는 상한이 안 된다 —
  # 큰 것부터 읽으므로 마감 직전에 4.6GB 짜리가 32개 떠 있을 수 있고, 그것들이
  # 끝날 때까지 몇 분이 더 간다. 실제로 상한 600초를 17분까지 넘겼고 그 사이
  # 가디언이 부팅 중인 워커를 죽였다.
  #
  # 진행 표시는 1초마다 깨어나 플래그를 보고 60초마다 한 줄 찍는다. sleep 60
  # 한 번으로 재우고 나중에 kill 하면 안 된다 — 서브셸만 죽고 자식 sleep 은
  # init 에 입양돼 cloud-init 이 끝난 뒤까지 상속받은 fd 를 붙들고 남는다.
  : > "$done"; : > "$flag"
  s0=$(rdsect)
  ( n=0
    while [ -e "$flag" ]; do
      sleep 1; n=$(( n + 1 ))
      if [ $(( n % 60 )) -eq 0 ]; then
        echo "[warm] 진행 $(wc -l < "$done")/${files}개, $(( ($(rdsect) - s0) / 2048 ))MB, ${n}초"
      fi
    done ) &
  mon=$!
  sort -rn "$list" | cut -f2- \
    | WARM_DEADLINE=$deadline WARM_DONE=$done xargs -d '\n' -P "$WARM_JOBS" -n 1 sh -c '
        rem=$(( WARM_DEADLINE - $(date +%s) ))
        [ "$rem" -gt 0 ] || exit 0
        timeout "$rem" dd if="$1" of=/dev/null bs=4M status=none 2>/dev/null || exit 0
        printf "%s\n" "$1" >> "$WARM_DONE"
      ' _ || true
  rm -f "$flag"
  wait "$mon" 2>/dev/null || true

  el=$(( $(date +%s) - t0 ))
  if [ "$el" -lt 1 ]; then el=1; fi
  disk_mb=$(( ($(rdsect) - s0) / 2048 ))
  read_bytes=$(awk -F'\t' 'NR==FNR{sz[$2]=$1; next} ($0 in sz){t+=sz[$0]} END{print t+0}' "$list" "$done")
  mbps=$(( disk_mb / el ))
  echo "[warm] 디스크 ${disk_mb}MB, ${el}초, 약 ${mbps} MB/s (끝까지 읽은 파일 $(wc -l < "$done")/${files}개 $(( read_bytes / 1048576 ))MB)"
  if [ "$el" -ge "$WARM_TIMEOUT" ]; then
    echo "[warm] 상한에 걸렸다 — 나머지 약 $(( bytes / 1048576 - disk_mb ))MB 는 첫 요청 때 읽힌다"
  fi
  set -x
  return 0
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
