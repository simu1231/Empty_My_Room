#!/bin/bash
# 6단계 — 동시 요청 부하 테스트. 스케일투제로 전 구간을 한 번에 잰다.
#
# ■ 재는 것 네 가지
#   ① 0대에서 첫 결과까지 — 사용자가 실제로 기다리는 값
#   ② 백로그 알람이 2호기를 띄우는지, 띄우면 처리량이 정말 늘어나는지
#   ③ 기동 뒤 워밍(warm-bg)이 이번엔 **읽기까지 가는지**. 워커 9호는 목록을
#      만드는 find 가 끝나기 전에 유휴 120초로 회수돼서 0바이트였다. 작업이
#      큐에 차 있으면 리퍼가 안 걷어가므로 이번엔 끝까지 간다.
#   ④ IOSchedulingClass=idle 이 옳은지. 첫 sam3d 가 스모크 기준 187초보다
#      **느려지면** 워밍이 추론과 경합하는 것이고, 비슷하거나 빠르면 아니다.
#      근거 없이 못 정한다고 config.sh 에 적어둔 바로 그 항목이다.
#
# ■ 왜 API 를 안 거치고 SQS 에 직접 넣는가
#   워커 AMI 는 api 컨테이너를 scale: 0 으로 띄운다(docker-compose.aws.yml).
#   API 는 아직 어디에도 배포돼 있지 않다. 그런데 워커도 오토스케일링도 보는
#   것은 SQS 뿐이라, 큐에 직접 넣으면 API 없이 전 구간을 잴 수 있다.
#
#   딱 하나 빠지는 게 있다 — api/main.py 는 큐에 넣은 **직후**
#   capacity.request_capacity() 로 ASG 를 직접 깨운다. 잠든 큐의 CloudWatch
#   지표는 다시 흐르기까지 최대 15분 늦기 때문이다(capacity.py 주석). 그 한
#   줄을 빼먹으면 이 테스트는 "15분 늦게 뜨더라"를 재게 된다 — 운영과 다른
#   값이다. 그래서 4단계에서 같은 호출을 그대로 재현한다.
#   --no-wake 를 주면 일부러 빼고 CloudWatch 경로만 잰다(그건 별도 실험이다).
#
# ■ 비용
#   ASG 최대가 스팟 2대(MAX_SPOT)라 상한이 박혀 있다. g6.xlarge 스팟 2대를
#   20분 돌려도 $0.2 아래다. 끝나면 리퍼가 유휴 120초에 스스로 내려간다 —
#   이 스크립트는 **아무것도 종료시키지 않는다**. 축소까지가 측정 대상이다.
set -uo pipefail
cd "$(dirname "$0")"

# ── aws 부재를 먼저 가른다 ───────────────────────────────────────────────
# config.sh 는 계정 ID 해시로 버킷 이름을 만든다. aws 가 PATH 에 없으면 조용히
# emr-jobs(존재하지 않는 버킷)로 떨어지고, 업로드가 NoSuchBucket 으로 죽는데
# 원인은 버킷이 아니라 PATH 다. 20-launch-template.sh 에서 똑같이 당했다.
command -v aws >/dev/null 2>&1 || {
  echo "✗ aws CLI 를 찾을 수 없습니다 (PATH=$PATH)"
  echo "  이 PC 에서는 ~/.local/bin 에 있다: export PATH=\"\$HOME/.local/bin:\$PATH\""
  exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "✗ python3 가 필요합니다"; exit 1; }

source ./config.sh

SESSIONS=2          # 사진 몇 장치를 넣을지. 1장 = sam3d 3 + scene 4 (capacity.py)
INTERVAL=5
TIMEOUT=2400
WAKE=1
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    -s|--sessions) SESSIONS=$2; shift 2;;
    -i|--interval) INTERVAL=$2; shift 2;;
    -t|--timeout)  TIMEOUT=$2; shift 2;;
    --no-wake)     WAKE=0; shift;;
    --dry-run)     DRY=1; shift;;
    -h|--help)
      sed -n '2,30p' "$0"; echo
      echo "사용법: $0 [-s 세션수=2] [-i 폴링초=5] [-t 제한초=2400] [--no-wake] [--dry-run]"
      exit 0;;
    *) echo "✗ 모르는 인자: $1"; exit 1;;
  esac
done

RUN=/tmp/emr-loadtest-$(date +%Y%m%d-%H%M%S)
mkdir -p "$RUN"
echo "▶ 기록 디렉터리: $RUN"

# ── 1. 사전 점검 ─────────────────────────────────────────────────────────
# 돈 쓰기 전에 멈출 수 있는 것은 전부 여기서 멈춘다.
echo "▶ 1. 사전 점검"
aws s3api head-bucket --bucket "$S3_BUCKET" >/dev/null 2>&1 || {
  echo "✗ 버킷 $S3_BUCKET 에 접근할 수 없습니다"; exit 1; }
echo "  버킷 $S3_BUCKET"

Q_SAM3D_URL=$(aws sqs get-queue-url --queue-name "$Q_SAM3D" --query QueueUrl --output text) || exit 1
Q_SCENE_URL=$(aws sqs get-queue-url --queue-name "$Q_SCENE" --query QueueUrl --output text) || exit 1

qdepth() {  # 큐 URL → "보이는것 처리중" 두 숫자
  aws sqs get-queue-attributes --queue-url "$1" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
    --query 'Attributes.[ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible]' \
    --output text 2>/dev/null || echo "? ?"
}
read -r S_VIS S_INF <<<"$(qdepth "$Q_SAM3D_URL")"
read -r C_VIS C_INF <<<"$(qdepth "$Q_SCENE_URL")"
echo "  큐 $Q_SAM3D $S_VIS/$S_INF, $Q_SCENE $C_VIS/$C_INF"
if [ "$S_VIS$S_INF$C_VIS$C_INF" != "0000" ]; then
  echo "  ⚠ 큐가 비어 있지 않다 — 남은 메시지가 측정에 섞인다."
  echo "    앞선 테스트의 잔여물이면 먼저 비울 것:"
  echo "      aws sqs purge-queue --queue-url $Q_SAM3D_URL"
  echo "      aws sqs purge-queue --queue-url $Q_SCENE_URL"
  exit 1
fi

asg_desired() { aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$1" \
                  --query 'AutoScalingGroups[0].DesiredCapacity' --output text 2>/dev/null; }
asg_insts()   { aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$1" \
                  --query 'AutoScalingGroups[0].Instances[].[InstanceId,LifecycleState]' \
                  --output text 2>/dev/null; }
D_SPOT=$(asg_desired "$ASG_SPOT"); D_OD=$(asg_desired "$ASG_OD")
echo "  ASG $ASG_SPOT desired=$D_SPOT / $ASG_OD desired=$D_OD"
if [ "$D_SPOT" != "0" ] || [ "$D_OD" != "0" ]; then
  echo "  ⚠ 이미 떠 있는 워커가 있다. 콜드스타트를 못 잰다 — 0대가 될 때까지 기다릴 것."
  exit 1
fi

# ── 2. 입력 이미지 ───────────────────────────────────────────────────────
# 이 PC 에는 PIL 이 없다. test_e2e.py 는 PIL 로 64x64 를 그려 썼고 세 모델이
# 전부 통과했으니, 같은 크기를 zlib+struct 로 직접 만든다(의존성 0).
echo "▶ 2. 입력 이미지 생성"
python3 - "$RUN/in.png" <<'PY'
import sys, zlib, struct
W = H = 64
raw = bytearray()
for y in range(H):
    raw.append(0)                      # 필터 타입 None
    for x in range(W):
        inside = 12 <= x < 52 and 12 <= y < 52
        raw += bytes((200, 120, 60) if inside else (30, 30, 40))
def chunk(tag, data):
    body = tag + data
    return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xffffffff)
png = (b'\x89PNG\r\n\x1a\n'
       + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0))
       + chunk(b'IDAT', zlib.compress(bytes(raw), 9))
       + chunk(b'IEND', b''))
open(sys.argv[1], 'wb').write(png)
PY
RUN_ID=$(python3 -c 'import uuid;print(uuid.uuid4().hex[:12])')
IMG_KEY="input/loadtest-$RUN_ID/image/in.png"
# 작업마다 올리지 않고 한 번만 올린다. 워커는 어차피 작업마다 S3 에서 새로
# 받으므로(worker.py download_file) 다운로드 횟수는 운영과 같다.
aws s3api put-object --bucket "$S3_BUCKET" --key "$IMG_KEY" \
  --body "$RUN/in.png" --content-type image/png >/dev/null || exit 1
echo "  s3://$S3_BUCKET/$IMG_KEY ($(wc -c < "$RUN/in.png") 바이트)"

# ── 3. 작업 투입 ─────────────────────────────────────────────────────────
# 구성은 운영의 한 세션을 그대로 흉내 낸다 — capacity.py 주석의
# "사진 1장 = sam3d 3 + scene 4". scene 4 는 방 레이아웃 1 + 물체 3 으로 본다.
echo "▶ 3. 작업 투입 (세션 ${SESSIONS}장 분량)"
python3 - "$RUN" "$IMG_KEY" "$SESSIONS" "$DDB_TABLE" <<'PY'
import json, sys, time, uuid, os
run, img_key, sessions, table = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
MIX = ["sam3d_mesh"] * 3 + ["room_layout"] + ["omni3d"] * 3
# 작업 종류마다 필요한 파라미터가 다르다. 전부 빈 dict 로 보냈다가
# omni3d 6건이 전부 "필요한 파라미터 누락: ['bbox','category']" 로 죽었다
# (worker.py _run_omni3d). 값은 test_e2e.py 가 쓰던, 통과가 확인된 것과 같다.
# bbox 는 위에서 그린 64x64 안의 밝은 사각형(12..52)을 가리킨다.
#
# 이건 테스트만의 문제가 아니었다. 실패한 작업은 가시성 시간이 지나면 큐로
# 돌아오고, 백로그 알람은 ">0" 에 걸려 있다. 그래서 절대 성공 못 할 6건이
# DLQ 로 빠질 때까지 함대를 계속 깨웠다 — 테스트가 끝난 뒤에도 인스턴스가
# 한 대 더 떠서 아무 일도 안 하고 내려갔다. 못 고치는 작업은 빨리 죽여야 싸다.
PARAMS = {
    "omni3d":      {"bbox": "10,10,54,54", "category": "액자"},
    "room_layout": {"camera_height_m": "1.6"},
}
now = int(time.time())
jobs, ddb_items, keys = [], [], []
for s in range(sessions):
    for t in MIX:
        jid = uuid.uuid4().hex
        prm = PARAMS.get(t, {})
        msg = {"job_id": jid, "job_type": t, "input_key": img_key,
               "input_keys": {"image": img_key}, "params": prm}
        jobs.append(msg)
        ddb_items.append({**{k: {"S": v} for k, v in
                             (("job_id", jid), ("job_type", t), ("input_key", img_key))},
                          "input_keys": {"M": {"image": {"S": img_key}}},
                          "params": {"M": {k: {"S": v} for k, v in prm.items()}},
                          "status": {"S": "queued"},
                          "created_at": {"N": str(now)},
                          "updated_at": {"N": str(now)},
                          # TTL 7일. API 와 같은 값으로 둔다 — 테스트 레코드만
                          # 영원히 남으면 나중에 테이블을 보고 헷갈린다.
                          "expires_at": {"N": str(now + 7 * 24 * 3600)}})
        keys.append({"job_id": {"S": jid}})
for i, (m, it) in enumerate(zip(jobs, ddb_items)):
    json.dump(it, open(f"{run}/ddb-{i:03d}.json", "w"))
    json.dump(m, open(f"{run}/msg-{i:03d}.json", "w"))
    open(f"{run}/jobs.tsv", "a").write(f"{i:03d}\t{m['job_id']}\t{m['job_type']}\n")
# 상태 조회는 batch-get-item 한 번으로 끝낸다. 작업마다 get-item 을 치면
# 폴링 한 바퀴가 14번의 왕복이 되어, 재려는 시간보다 재는 비용이 커진다.
json.dump({table: {"Keys": keys,
                   "ProjectionExpression": "job_id,#s,elapsed_sec,#e",
                   "ExpressionAttributeNames": {"#s": "status", "#e": "error"}}},
          open(f"{run}/batchget.json", "w"))
print(f"  {len(jobs)}건 — sam3d {sum(1 for j in jobs if j['job_type']=='sam3d_mesh')}, "
      f"room_layout {sum(1 for j in jobs if j['job_type']=='room_layout')}, "
      f"omni3d {sum(1 for j in jobs if j['job_type']=='omni3d')}")
PY
N=$(wc -l < "$RUN/jobs.tsv")

if [ "$DRY" -eq 1 ]; then
  echo "▶ --dry-run — 여기까지. 투입하지 않았다. 생성물은 $RUN 에 있다."
  exit 0
fi

# 큐 배정은 api/main.py 의 QUEUE_NAMES 와 같아야 한다.
#   sam3d_mesh → emr-sam3d / room_layout, omni3d → emr-scene
queue_for() { case "$1" in sam3d_mesh) echo "$Q_SAM3D_URL";; *) echo "$Q_SCENE_URL";; esac; }

T0=$(date +%s)
echo "$T0 투입시작" >> "$RUN/timeline.txt"
while IFS=$'\t' read -r idx jid jtype; do
  # DDB 를 **먼저** 쓴다. 큐에 먼저 넣으면 워커가 즉시 집어갔을 때 조회할
  # 레코드가 없다 — api/main.py 가 같은 순서인 이유다.
  aws dynamodb put-item --table-name "$DDB_TABLE" \
    --item "file://$RUN/ddb-$idx.json" >/dev/null || { echo "✗ DDB 쓰기 실패 $jid"; exit 1; }
  aws sqs send-message --queue-url "$(queue_for "$jtype")" \
    --message-body "file://$RUN/msg-$idx.json" >/dev/null || { echo "✗ SQS 전송 실패 $jid"; exit 1; }
done < "$RUN/jobs.tsv"
echo "  $N 건 투입 완료 ($(( $(date +%s) - T0 ))초 소요)"

# ── 4. 용량 깨우기 ───────────────────────────────────────────────────────
# capacity.py 의 _nudge() 를 그대로 옮긴 것이다: desired 가 0 이면 1 로 올리고,
# HonorCooldown=False 로 방금 축소한 직후에도 즉시 올린다.
if [ "$WAKE" -eq 1 ]; then
  echo "▶ 4. 용량 요청 (API 의 capacity.request_capacity() 재현)"
  aws autoscaling set-desired-capacity --auto-scaling-group-name "$ASG_SPOT" \
    --desired-capacity 1 --no-honor-cooldown && echo "  $ASG_SPOT desired 0 → 1"
  echo "$(date +%s) 용량요청 desired=1" >> "$RUN/timeline.txt"
else
  echo "▶ 4. 용량 요청 **생략** (--no-wake) — CloudWatch 백로그 알람만으로 뜨는지 본다."
  echo "  잠든 큐의 지표는 최대 15분 늦을 수 있다. 오래 걸려도 고장이 아니다."
  echo "$(date +%s) 용량요청생략" >> "$RUN/timeline.txt"
fi

# ── 5. 추적 ──────────────────────────────────────────────────────────────
echo "▶ 5. 추적 (${INTERVAL}초 간격, 최대 ${TIMEOUT}초). Ctrl-C 해도 워커는 리퍼가 내린다."
printf '%7s  %-28s  %-13s  %-11s  %s\n' 경과 "작업(큐:대기/처리중)" "ASG" "인스턴스" 변화
declare -A STATE_OF
PREV_SIG=""; LAST_CONSOLE=0; MAX_RUNNING=0; SEEN_INST=""
while :; do
  EL=$(( $(date +%s) - T0 ))
  [ "$EL" -gt "$TIMEOUT" ] && { echo "  ⚠ 제한 시간 ${TIMEOUT}초 초과 — 추적을 멈춘다"; break; }

  # 상태 한 방에 읽기
  aws dynamodb batch-get-item --request-items "file://$RUN/batchget.json" \
    --output json > "$RUN/.states.json" 2>/dev/null
  mapfile -t LINES < <(python3 - "$RUN/.states.json" "$DDB_TABLE" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for it in d.get("Responses", {}).get(sys.argv[2], []):
    print(it["job_id"]["S"], it.get("status", {}).get("S", "?"),
          it.get("elapsed_sec", {}).get("N", ""), sep="\t")
PY
)
  NQ=0; NR=0; ND=0; NE=0
  for ln in "${LINES[@]}"; do
    IFS=$'\t' read -r jid st el <<<"$ln"
    if [ "${STATE_OF[$jid]:-}" != "$st" ]; then
      STATE_OF[$jid]=$st
      echo "$EL $jid $st $el" >> "$RUN/transitions.txt"
    fi
    case "$st" in queued) NQ=$((NQ+1));; running) NR=$((NR+1));;
                  done) ND=$((ND+1));; *) NE=$((NE+1));; esac
  done
  [ "$NR" -gt "$MAX_RUNNING" ] && MAX_RUNNING=$NR

  read -r S_VIS S_INF <<<"$(qdepth "$Q_SAM3D_URL")"
  read -r C_VIS C_INF <<<"$(qdepth "$Q_SCENE_URL")"
  DS=$(asg_desired "$ASG_SPOT"); DO=$(asg_desired "$ASG_OD")
  INST=$(asg_insts "$ASG_SPOT"; asg_insts "$ASG_OD")
  NIN=$(printf '%s' "$INST" | grep -c 'InService' || true)
  NTOT=$(printf '%s\n' "$INST" | grep -c 'i-' || true)

  SIG="$NQ/$NR/$ND/$NE|$S_VIS:$S_INF|$C_VIS:$C_INF|$DS:$DO|$NIN/$NTOT"
  if [ "$SIG" != "$PREV_SIG" ]; then
    printf '%6ss  대기%-2s 진행%-2s 완료%-2s 실패%-2s  s%s:%s c%s:%s  %s+%s  %s/%s대 가동\n' \
      "$EL" "$NQ" "$NR" "$ND" "$NE" "$S_VIS" "$S_INF" "$C_VIS" "$C_INF" "$DS" "$DO" "$NIN" "$NTOT"
    echo "$(date +%s) $SIG" >> "$RUN/timeline.txt"
    PREV_SIG=$SIG
  fi

  # 새 인스턴스를 기억해 둔다(나중에 콘솔 로그를 긁는다)
  #
  # timeline.txt 의 첫 칸은 **에폭 초**다. 요약이 거기서 T0 를 빼서 경과를 만든다.
  # 여기만 $EL(이미 뺀 값)을 적었다가 "-1790874778s 인스턴스 등장"이 찍혔다.
  # 두 번 빼면 1970년이 나온다. 적는 쪽을 다른 줄과 같은 모양으로 맞춘다.
  while read -r iid _; do
    case "$iid" in i-*) case " $SEEN_INST " in *" $iid "*) :;; *) SEEN_INST="$SEEN_INST $iid"
      echo "$(date +%s) 인스턴스 등장 $iid" >> "$RUN/timeline.txt";; esac;; esac
  done <<<"$INST"

  # 콘솔 로그는 60초마다만 긁는다. EC2 쪽 갱신 주기가 분 단위라 더 자주 불러야
  # 같은 내용만 온다. warm-bg 가 journal+console 로 내보내는 줄이 여기 찍힌다.
  if [ -n "$SEEN_INST" ] && [ $(( $(date +%s) - LAST_CONSOLE )) -ge 60 ]; then
    LAST_CONSOLE=$(date +%s)
    for iid in $SEEN_INST; do
      aws ec2 get-console-output --instance-id "$iid" --latest \
        --query Output --output text 2>/dev/null | base64 -d > "$RUN/console-$iid.txt" 2>/dev/null
    done
    # 새로 나타난 warm-bg 줄만 화면에 올린다
    cat "$RUN"/console-*.txt 2>/dev/null | grep -h 'warm-bg' | sort -u > "$RUN/.warm.now" || true
    if ! diff -q "$RUN/.warm.seen" "$RUN/.warm.now" >/dev/null 2>&1; then
      comm -13 <(sort -u "$RUN/.warm.seen" 2>/dev/null) <(sort -u "$RUN/.warm.now") \
        | sed 's/^/         ▸ /'
      cp "$RUN/.warm.now" "$RUN/.warm.seen"
    fi
  fi

  [ "$ND" -eq "$N" ] && { echo "  ✔ 전부 완료 (${EL}초)"; break; }
  [ $((ND + NE)) -eq "$N" ] && { echo "  ⚠ 완료 $ND / 실패 $NE (${EL}초)"; break; }
  sleep "$INTERVAL"
done

# ── 6. 요약 ──────────────────────────────────────────────────────────────
echo
python3 - "$RUN" "$MAX_RUNNING" <<'PY'
import os, sys, collections
run, max_running = sys.argv[1], int(sys.argv[2])
jt = {}
for ln in open(f"{run}/jobs.tsv"):
    i, jid, t = ln.rstrip("\n").split("\t"); jt[jid] = t
at = collections.defaultdict(dict)
for ln in open(f"{run}/transitions.txt"):
    el, jid, st, *rest = ln.split()
    at[jid].setdefault(st, int(el))
print("=" * 72)
print(f"{'작업':<12}{'대기→시작':>10}{'시작→완료':>10}{'투입→완료':>10}")
print("-" * 72)
per = collections.defaultdict(list)
for jid, t in jt.items():
    a = at.get(jid, {})
    r, d = a.get("running"), a.get("done")
    wait = r if r is not None else None
    proc = (d - r) if (r is not None and d is not None) else None
    print(f"{t:<12}{(str(wait)+'s') if wait is not None else '—':>10}"
          f"{(str(proc)+'s') if proc is not None else '—':>10}"
          f"{(str(d)+'s') if d is not None else '미완':>10}")
    if proc is not None: per[t].append(proc)
print("-" * 72)
for t, v in per.items():
    print(f"{t:<12} 건수 {len(v):<3} 최초 {v[0]}s  최소 {min(v)}s  중앙 {sorted(v)[len(v)//2]}s")
print("=" * 72)
dones = [a["done"] for a in at.values() if "done" in a]
runs  = [a["running"] for a in at.values() if "running" in a]
if runs: print(f"첫 작업 시작   : {min(runs)}s   (= 0대에서 워커가 일을 집기까지)")
if dones:
    print(f"첫 결과        : {min(dones)}s   (= 사용자 체감 첫 응답)")
    print(f"전체 완료      : {max(dones)}s")
# 워커가 몇 대 실제로 **일했는지**. DDB 레코드에는 어느 인스턴스가 처리했는지가
# 안 들어 있어서(set_status 가 안 쓴다) 직접은 못 센다. 대신 워커는 SQS 를
# MaxNumberOfMessages=1 로 폴링하므로 컨테이너 하나당 동시 1건이 상한이다.
# 인스턴스마다 worker-sam3d 와 worker-scene 이 하나씩 도니, 동시 running 의
# 최댓값은 "가동 중인 워커 컨테이너 수"와 같다 — 인스턴스 수의 하한이 된다.
print(f"최대 동시 처리 : {max_running}건  (워커 컨테이너 기준. 인스턴스 1대당 최대 2)")
print()
tl = [l.split(None, 1) for l in open(f"{run}/timeline.txt") if "|" in l or "desired" in l or "인스턴스" in l]
if tl:
    base = int(tl[0][0])
    print("타임라인(ASG/큐):")
    for ts, rest in tl:
        print(f"  {int(ts)-base:>5}s  {rest.strip()}")
    print()
print("warm-bg 콘솔:")
import glob
lines = sorted({l.rstrip() for f in glob.glob(f"{run}/console-*.txt") for l in open(f, errors="replace") if "warm-bg" in l})
print("\n".join("  " + l for l in lines) if lines else "  (한 줄도 없음 — 콘솔 갱신이 늦었거나 워밍이 못 떴다)")
PY
echo
echo "▶ 전체 기록: $RUN"
echo "  워커는 리퍼가 유휴 ${IDLE_EXIT_SEC:-120}초 뒤 스스로 내린다. 확인:"
echo "    aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names $ASG_SPOT \\"
echo "      --query 'AutoScalingGroups[0].[DesiredCapacity,Instances[].LifecycleState]'"
