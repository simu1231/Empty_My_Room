#!/bin/bash
# 요금 폭주 차단용 감시자. 인스턴스 **호스트**에서 systemd 타이머로 1분마다 돈다.
#
# 왜 필요한가 — 리퍼(idle_reaper.py)는 컴포즈 스택 안의 컨테이너다. 그래서
# `docker compose up -d`가 실패하면 리퍼도 안 뜬다. 그런데 ASG 헬스체크는
# --health-check-type EC2 라서 "인스턴스가 켜져 있는지"만 본다. 결과:
#
#   컴포즈 실패 → 리퍼 없음 → 아무도 인스턴스를 회수하지 않음
#   → ASG는 정상으로 판단 → g6.xlarge가 일 없이 영원히 과금($0.45/h = 월 $327)
#
# 즉 스케일투제로 장치가 자기가 보호해야 할 대상에 의존하고 있었다. 가디언은
# 컴포즈 바깥(호스트 systemd)에 두어 그 고리를 끊는다. 하는 일은 하나 —
# "리퍼가 살아있나"만 보고, 오래 없으면 인스턴스를 회수한다.
#
# 일부러 단순하게 뒀다. 작업 중인지 판단하는 건 리퍼의 일이고, 가디언이 그걸
# 또 구현하면 버그 날 곳이 두 배가 된다. 가디언의 전제는 이거다:
#   "리퍼가 없으면 이 인스턴스는 스스로 물러날 방법이 없다 → 물러나게 한다."
# 처리 중이던 작업은 SQS 가시성 타임아웃이 끝나면 큐로 돌아가 재시도된다
# (워커가 하트비트로 가시성을 늘리므로, 죽으면 자동으로 되돌아온다).
set -uo pipefail

STATE=${GUARDIAN_STATE_DIR:-/var/lib/emr}   # 테스트에서 덮어쓸 수 있게
FAILFILE=$STATE/guardian.fail
GRACE_SEC=${GUARDIAN_GRACE_SEC:-900}
FAIL_MIN=${GUARDIAN_FAIL_MIN:-5}
DRY_RUN=${GUARDIAN_DRY_RUN:-0}
REAPER_NAME=${GUARDIAN_REAPER_NAME:-reaper}

mkdir -p "$STATE"
log() { echo "[guardian] $*"; }

# ── 부팅 유예 ────────────────────────────────────────────────────────────
# 부팅 직후에는 컴포즈가 아직 올라오는 중이다. 여기서 안 봐주면 정상 기동도
# 죽인다. uptime을 쓰는 이유: 타이머가 재시작돼도 값이 안 리셋된다.
UP=$(cut -d. -f1 /proc/uptime)
if [ "$UP" -lt "$GRACE_SEC" ]; then
  log "부팅 후 ${UP}초 — 유예 ${GRACE_SEC}초 이내라 판단 보류"
  exit 0
fi

# ── 리퍼 생존 확인 ───────────────────────────────────────────────────────
# docker 자체가 죽은 경우도 "리퍼 없음"으로 본다(그게 사실이다).
ALIVE=0
if command -v docker >/dev/null 2>&1; then
  if docker ps --filter "name=$REAPER_NAME" --filter "status=running" --format '{{.Names}}' 2>/dev/null \
       | grep -q .; then
    ALIVE=1
  fi
fi

if [ "$ALIVE" -eq 1 ]; then
  if [ -f "$FAILFILE" ]; then
    log "리퍼 복귀 — 카운터 초기화"
    rm -f "$FAILFILE"
  fi
  exit 0
fi

# ── 카운트 ───────────────────────────────────────────────────────────────
N=$(( $(cat "$FAILFILE" 2>/dev/null || echo 0) + 1 ))
echo "$N" > "$FAILFILE"
log "리퍼 없음 ${N}/${FAIL_MIN}분"
[ "$N" -lt "$FAIL_MIN" ] && exit 0

# ── 회수 ─────────────────────────────────────────────────────────────────
log "리퍼가 ${FAIL_MIN}분간 없다 — 이 인스턴스는 스스로 물러날 수 없다. 회수한다."

# 실제 종료는 self-retire.sh 하나에만 있다. userdata 실패 트랩도 같은 파일을
# 쓴다 — --should-decrement-desired-capacity 같은 걸 한쪽만 고치는 사고를 막는다.
RETIRE=${GUARDIAN_RETIRE_BIN:-/opt/emr/bin/self-retire.sh}
if [ ! -x "$RETIRE" ]; then
  log "치명적: $RETIRE 가 없다. AMI가 잘못 구워졌다."
  exit 1
fi
EMR_RETIRE_DRY_RUN=$DRY_RUN exec "$RETIRE" "리퍼 부재 ${FAIL_MIN}분"
