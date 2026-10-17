#!/usr/bin/env bash
# 표 5/6 전체 실행: 표 6 → 조건 a → (사이드카 기동) → 나머지 조건 → (사이드카 종료)
# 단계마다 GPU 가 정리됐는지 확인하고, 비정상이면 멈춘다. 실패한 단계는 재실행하지 않는다.
set -u
REPO="$HOME/Empty_My_Room"
PY="$HOME/miniconda3/envs/sam3d/bin/python"
OUT="${1:-$REPO/eval/timing_results/gpucontention_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT"
LOG="$OUT/driver.log"
say(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
port_pid(){ ss -ltnp 2>/dev/null | grep ":$1 " | grep -oP 'pid=\K[0-9]+' | head -1; }
gpu_used(){ nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1 | tr -d ' '; }
SIDECARS_STARTED=0

stop_sidecars(){
  for p in 8002 8003; do pid=$(port_pid $p); [ -n "$pid" ] && kill "$pid" 2>/dev/null && say "사이드카 :$p 종료 (pid $pid)"; done
  sleep 5
}
fail(){ say "!! $*"; [ "$SIDECARS_STARTED" = 1 ] && stop_sidecars; say "PHASE ABORTED"; exit 1; }
wait_gpu_clean(){  # $1 = 허용 상한(MiB). 사이드카가 떠 있으면 그 몫을 포함한 값을 준다.
  for i in $(seq 1 20); do u=$(gpu_used); [ "$u" -le "$1" ] && { say "GPU 정리 확인: ${u} MiB (상한 $1)"; return 0; }; sleep 2; done
  fail "GPU 가 정리되지 않았다: ${u} MiB > $1"
}
wait_port(){ for i in $(seq 1 "$2"); do [ -n "$(port_pid $1)" ] && return 0; sleep 1; done; return 1; }

say "PHASE 0: 사전 점검"
for p in 8001 8002 8003 5173; do [ -n "$(port_pid $p)" ] && fail "포트 :$p 가 열려 있다 — 서비스가 모두 꺼진 상태여야 한다"; done
wait_gpu_clean 1500
say "출력 $OUT / dxg 누적(부팅 이후) $(dmesg 2>/dev/null | grep -c 'dxgkio_create_allocation')"

if [ "${START_PHASE:-0}" -le 2 ]; then
say "PHASE 1: 표 6 (SD 콜드/웜)"
"$PY" "$REPO/tools/timing/measure_sd_cost.py" --out "$OUT/table6" > "$OUT/table6.stdout" 2>&1 || fail "표 6 실패 (종료코드 $?) — $OUT/table6.stdout 확인"
say "표 6 완료"
wait_gpu_clean 1500

say "PHASE 2: 조건 a (사이드카 없음)"
"$PY" "$REPO/tools/timing/measure_gpu_contention.py" --conditions a --out "$OUT/table5" > "$OUT/table5_a.stdout" 2>&1 || fail "조건 a 실행 실패 (종료코드 $?) — $OUT/table5_a.stdout 확인"
say "조건 a 완료"
wait_gpu_clean 1500
else say "PHASE 1~2 건너뜀(START_PHASE=${START_PHASE}) — 기존 결과 유지"; fi

say "PHASE 3: 사이드카 기동"
set +u   # omni3d env 의 activate-binutils 스크립트가 set -u 에서 ADDR2LINE 미정의로 실패한다(services.sh 와 같은 이유)
source "$HOME/miniconda3/etc/profile.d/conda.sh"
SIDECARS_STARTED=1
( cd "$HOME/uLayout" && conda activate uLayout && nohup python server.py > "$OUT/uLayout.log" 2>&1 & )
wait_port 8002 240 || fail "uLayout :8002 기동 실패"
( cd "$HOME/omni3d" && conda activate omni3d && LD_LIBRARY_PATH="$HOME/miniconda3/envs/omni3d/lib/python3.10/site-packages/torch/lib" nohup python server.py > "$OUT/Omni3D.log" 2>&1 & )
wait_port 8003 240 || fail "Omni3D :8003 기동 실패"
sleep 5
say "uLayout health: $(curl -s -m5 localhost:8002/health)"; say "Omni3D health: $(curl -s -m5 localhost:8003/health)"
say "사이드카 유휴 상태 GPU: $(gpu_used) MiB"

say "PHASE 4: 조건 b, c1, c2, d, e, f1, f2, g1, g2, b_end"
"$PY" "$REPO/tools/timing/measure_gpu_contention.py" --conditions b,c1,c2,d,e,f1,f2,g1,g2,b_end --out "$OUT/table5" > "$OUT/table5_rest.stdout" 2>&1; rc=$?
[ $rc -ne 0 ] && say "!! 조건 묶음 종료코드 $rc — $OUT/table5_rest.stdout 확인(결과는 raw.csv 에 남아 있다)"

say "PHASE 5: 사이드카 종료"
stop_sidecars
SIDECARS_STARTED=0
wait_gpu_clean 1500
say "DONE"
