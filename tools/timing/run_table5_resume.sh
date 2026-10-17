#!/usr/bin/env bash
# g1 이후 이어서 실행: g2, b_end. 조건마다 사이드카를 새로 기동한다(g1 이 남긴 상태의 영향을 없애고 b 와 같은 출발 상태로 맞추기 위해).
# 실행 하나가 600초를 넘으면 측정 프로세스가 스스로 종료코드 97 로 죽는다(워치독). g1 과 중단된 조건은 재실행하지 않는다.
set -u
REPO="$HOME/Empty_My_Room"; PY="$HOME/miniconda3/envs/sam3d/bin/python"; OUT="$1"
LOG="$OUT/driver_resume.log"
say(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
port_pid(){ ss -ltnp 2>/dev/null | grep ":$1 " | grep -oP 'pid=\K[0-9]+' | head -1; }
gpu_used(){ nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1 | tr -d ' '; }
fail(){ say "!! $*"; stop_sidecars; say "ABORTED"; exit 1; }
stop_sidecars(){
  for p in 8002 8003; do pid=$(port_pid $p); [ -n "$pid" ] && kill "$pid" 2>/dev/null; done
  for i in $(seq 1 30); do [ -z "$(port_pid 8002)$(port_pid 8003)" ] && break; sleep 1; done
  for p in 8002 8003; do pid=$(port_pid $p); [ -n "$pid" ] && { say "사이드카 :$p SIGTERM 무시 → SIGKILL"; kill -9 "$pid"; }; done
  sleep 5
}
wait_port(){ for i in $(seq 1 "$2"); do [ -n "$(port_pid $1)" ] && return 0; sleep 1; done; return 1; }
start_sidecars(){
  set +u   # omni3d env 의 activate 스크립트가 set -u 에서 실패한다
  source "$HOME/miniconda3/etc/profile.d/conda.sh"
  ( cd "$HOME/uLayout" && conda activate uLayout && nohup python server.py > "$OUT/uLayout_$1.log" 2>&1 & )
  wait_port 8002 240 || fail "uLayout 기동 실패"
  ( cd "$HOME/omni3d" && conda activate omni3d && LD_LIBRARY_PATH="$HOME/miniconda3/envs/omni3d/lib/python3.10/site-packages/torch/lib" nohup python server.py > "$OUT/Omni3D_$1.log" 2>&1 & )
  wait_port 8003 240 || fail "Omni3D 기동 실패"
  sleep 6
  h1=$(curl -s -m8 localhost:8002/health); h2=$(curl -s -m8 localhost:8003/health)
  say "uLayout health: $h1 / Omni3D health: $h2 / 유휴 GPU $(gpu_used) MiB"
  echo "$h1$h2" | grep -q ulayout_loaded && echo "$h1$h2" | grep -q omni3d_loaded || fail "사이드카 health 비정상"
}
check_clean(){  # 측정 프로세스가 없고 GPU 가 사이드카 몫(상한 4000 MiB)만 남았는지
  alive=$(ps -eo pid,cmd | awk '$2 ~ /python/ && /measure_gpu_contention.py/ {print $1}' | tr '\n' ' ')
  [ -n "$alive" ] && { say "측정 프로세스가 남아 있다: $alive — 종료"; kill -9 $alive; sleep 3; }
  for i in $(seq 1 20); do u=$(gpu_used); [ "$u" -le 4000 ] && { say "정리 확인: 측정 프로세스 없음, GPU ${u} MiB"; return 0; }; sleep 2; done
  fail "GPU 가 정리되지 않았다: ${u} MiB > 4000"
}
run_cond(){
  say "PHASE $1: 사이드카 새로 기동"
  stop_sidecars; start_sidecars "$1"
  say "PHASE $1: 조건 실행(워치독 600초)"
  "$PY" "$REPO/tools/timing/measure_gpu_contention.py" --conditions "$1" --out "$OUT/table5" --watchdog-s 600 > "$OUT/table5_$1.stdout" 2>&1; rc=$?
  say "조건 $1 종료코드 $rc $( [ $rc -eq 97 ] && echo '(워치독이 강제 종료)' )"
  check_clean
}

say "RESUME 사전 점검"
for p in 8001 5173; do [ -n "$(port_pid $p)" ] && fail "포트 :$p 가 열려 있다"; done
check_clean
run_cond g2
run_cond b_end
say "PHASE 마무리: 사이드카 종료"
stop_sidecars
u=$(gpu_used); say "최종 GPU ${u} MiB / 사이드카 포트 $( [ -z "$(port_pid 8002)$(port_pid 8003)" ] && echo 닫힘 || echo 열림 )"
say "DONE"
