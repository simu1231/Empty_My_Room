#!/usr/bin/env bash
# Empty My Room — 서비스 4종 일괄 기동/종료/상태확인
#
#   backend   :8001  conda sam3d    ~/Empty_My_Room/sam3d/backend
#   uLayout   :8002  conda uLayout  ~/uLayout
#   Omni3D    :8003  conda omni3d   ~/omni3d        (LD_LIBRARY_PATH 필요)
#   frontend  :5173  npm(vite)      ~/Empty_My_Room/sam3d/frontend
#
# 사용법:  ./services.sh start | stop | status | logs <name>
#
# 이미 떠 있는 서비스는 건너뛴다(포트 충돌로 죽는 걸 방지). uvicorn은 모델을
# 전부 로드한 뒤에야 포트를 바인딩하므로, 포트 LISTEN 여부가 곧 준비 완료 신호다.

# set -u 는 쓰지 않는다 — sam3d 환경의 activate.d/activate-gcc_linux-64.sh 가 SYS_SYSROOT를
# 미정의 상태로 참조해서 conda activate 자체가 실패한다.
set -o pipefail

REPO=/home/tmvlem5671/Empty_My_Room
ULAYOUT_DIR=/home/tmvlem5671/uLayout
OMNI3D_DIR=/home/tmvlem5671/omni3d
OMNI3D_ENV=/home/tmvlem5671/miniconda3/envs/omni3d
LOG_DIR=/tmp/emr-logs

mkdir -p "$LOG_DIR"
source ~/miniconda3/etc/profile.d/conda.sh

port_pid() { ss -ltnp 2>/dev/null | grep ":$1 " | grep -oP 'pid=\K[0-9]+' | head -1; }

wait_port() {  # $1=port  $2=timeout(s)  $3=name
  local i=0
  while [ "$i" -lt "$2" ]; do
    if [ -n "$(port_pid "$1")" ]; then
      printf '  \033[32m✔\033[0m %-9s :%s  (%ss)\n' "$3" "$1" "$i"; return 0
    fi
    sleep 1; i=$((i+1))
  done
  printf '  \033[31m✘\033[0m %-9s :%s  기동 실패 → %s/%s.log 확인\n' "$3" "$1" "$LOG_DIR" "$3"
  return 1
}

skip_if_up() {  # $1=port $2=name
  local pid; pid=$(port_pid "$1")
  [ -z "$pid" ] && return 1
  printf '  \033[90m•\033[0m %-9s :%s  이미 실행 중 (pid %s)\n' "$2" "$1" "$pid"; return 0
}

start_backend() {
  skip_if_up 8001 backend && return 0
  ( cd "$REPO/sam3d/backend" \
    && conda activate sam3d \
    && nohup python -m uvicorn main:app --host 0.0.0.0 --port 8001 \
         > "$LOG_DIR/backend.log" 2>&1 & )
  wait_port 8001 300 backend   # SAM2 + LaMa 로드 대기
}

start_ulayout() {
  skip_if_up 8002 uLayout && return 0
  ( cd "$ULAYOUT_DIR" \
    && conda activate uLayout \
    && nohup python server.py > "$LOG_DIR/uLayout.log" 2>&1 & )
  wait_port 8002 180 uLayout
}

start_omni3d() {
  skip_if_up 8003 Omni3D && return 0
  # pytorch3d._C 로드에 conda env의 torch/lib이 LD_LIBRARY_PATH에 있어야 한다
  ( cd "$OMNI3D_DIR" \
    && conda activate omni3d \
    && LD_LIBRARY_PATH="$OMNI3D_ENV/lib/python3.10/site-packages/torch/lib" \
       nohup python server.py > "$LOG_DIR/Omni3D.log" 2>&1 & )
  wait_port 8003 180 Omni3D
}

start_frontend() {
  skip_if_up 5173 frontend && return 0
  ( cd "$REPO/sam3d/frontend" \
    && nohup npm run dev > "$LOG_DIR/frontend.log" 2>&1 & )
  wait_port 5173 120 frontend
}

cmd_start() {
  echo "▶ Empty My Room 기동"
  start_backend; start_ulayout; start_omni3d; start_frontend
  echo
  cmd_status
}

cmd_stop() {
  echo "■ 종료"
  for entry in "8001 backend" "8002 uLayout" "8003 Omni3D" "5173 frontend"; do
    set -- $entry
    local pid; pid=$(port_pid "$1")
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null && printf '  \033[33m-\033[0m %-9s :%s  종료 (pid %s)\n' "$2" "$1" "$pid"
    else
      printf '  \033[90m•\033[0m %-9s :%s  이미 꺼져있음\n' "$2" "$1"
    fi
  done
}

cmd_status() {
  echo "● 상태"
  for entry in "8001 backend" "8002 uLayout" "8003 Omni3D" "5173 frontend"; do
    set -- $entry
    local pid; pid=$(port_pid "$1")
    if [ -n "$pid" ]; then
      printf '  \033[32m✔\033[0m %-9s :%s  pid %-8s %s\n' "$2" "$1" "$pid" "$(ps -o etime= -p "$pid" | tr -d ' ')"
    else
      printf '  \033[31m✘\033[0m %-9s :%s  중지\n' "$2" "$1"
    fi
  done
  local health; health=$(curl -s --max-time 3 http://127.0.0.1:8001/health 2>/dev/null)
  [ -n "$health" ] && echo "  health: $health"
  command -v nvidia-smi >/dev/null && \
    echo "  GPU: $(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader)"
  echo
  echo "  → http://localhost:5173"
}

case "${1:-status}" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  restart) cmd_stop; sleep 3; cmd_start ;;
  status) cmd_status ;;
  logs)   tail -f "$LOG_DIR/${2:-backend}.log" ;;
  *) echo "usage: $0 {start|stop|restart|status|logs <backend|uLayout|Omni3D|frontend>}"; exit 1 ;;
esac
