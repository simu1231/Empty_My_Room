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

# 상태(실패 카운터)는 **tmpfs인 /run 에 둔다**. 영구 디스크(/var/lib)에 두면
# AMI를 굽는 순간의 카운터가 그대로 박힌다 — 실제로 그랬다. bake-ami.sh 가 지워도
# 그 **뒤에** 도는 smoke-test.sh 가 스택을 내리는 순간 가디언이 다시 쌓기 시작해서,
# 스냅샷에는 11이 들어간 채로 굳었다. 그 AMI로 뜨는 워커는 유예 15분이 끝나는
# 바로 그 순간 N=11+1 로 임계값을 넘어버려서, 설계상 더 봐주기로 한 5분이
# 통째로 사라진다. "지운 뒤에 다시 생기는" 걸 rm 으로 쫓아다니는 대신
# (verify-ami.sh 의 ~/.aws 건과 똑같은 함정이다) 부팅마다 비워지는 자리로
# 옮겨 구조적으로 막는다.
#
# systemd 의 RuntimeDirectory= 로 만들면 안 된다. 이 유닛은 Type=oneshot 이라
# 매분 실행이 끝날 때마다 유닛이 "정지"되고 RuntimeDirectory 도 같이 지워진다.
# 그러면 카운터가 1분마다 0으로 돌아가 영원히 FAIL_MIN 에 못 닿는다 — 가디언이
# 조용히 무력화된다. 그래서 디렉터리는 아래에서 mkdir 로 직접 만든다.
STATE=${GUARDIAN_STATE_DIR:-/run/emr}   # 테스트에서 덮어쓸 수 있게
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
