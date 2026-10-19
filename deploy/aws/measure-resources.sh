#!/bin/bash
# measure_resources.py 를 백그라운드로 켜고 끄는 얇은 래퍼. smoke-test.sh 가 쓴다.
#
#   bash measure-resources.sh start [라벨]   # 샘플링 시작, 출력 디렉터리를 찍는다
#   bash measure-resources.sh mark  구간이름  # 구간 표시(예: "sam3d-fal", "omni3d")
#   bash measure-resources.sh stop           # 중지 + 요약(report.md) 출력. 종료 코드 0 통과 / 3 미달 / 4 측정 없음·이상
#
# 출력: ${EMR_MEASURE_DIR:-/var/tmp/emr-measure-<시각>}/{samples.csv,marks.txt,report.md,report.json}
# 가장 최근 디렉터리는 /var/tmp/emr-measure.latest 에 적어 둔다.
set -uo pipefail
cd "$(dirname "$0")"
PY=python3
LATEST=${EMR_MEASURE_LATEST:-/var/tmp/emr-measure.latest}   # 테스트에서 덮어쓴다
case "${1:-}" in
  start)
    rm -f "$LATEST"   # 시작이 실패했을 때 stop 이 **지난 측정**을 읽어 통과시키는 일을 막는다
    OUT=${EMR_MEASURE_DIR:-/var/tmp/emr-measure-$(date +%Y%m%d-%H%M%S)}
    mkdir -p "$OUT"
    nohup $PY ./measure_resources.py sample --out "$OUT" --interval "${EMR_MEASURE_INTERVAL:-2}" \
          --label "${2:-}" >"$OUT/sampler.log" 2>&1 &
    echo $! > "$OUT/pid"
    echo "$OUT" > "$LATEST"
    echo "▶ 자원 측정 시작 → $OUT (pid $(cat "$OUT/pid"))" ;;
  mark)
    [ -f "$LATEST" ] || exit 0
    $PY ./measure_resources.py mark --out "$(cat "$LATEST")" "${2:-mark}" ;;
  stop)
    # 종료 코드: 0 = 기준 통과 / 3 = 기준 미달 / 4 = 측정 결과 없음·부족·측정기 이상.
    # smoke-test.sh 가 EMR_MEASURE_ENFORCE 로 이 코드를 실패로 볼지 정한다. 코드가 0 이 아니라고 해서
    # 여기서 smoke 가 실패하는 것은 아니다 — 판단은 호출하는 쪽이 한다.
    [ -f "$LATEST" ] || { echo "MEASURE_RESULT=error  (측정이 시작된 적이 없다)"; exit 4; }
    OUT=$(cat "$LATEST")
    DEAD=0
    if [ -f "$OUT/pid" ] && kill -0 "$(cat "$OUT/pid")" 2>/dev/null; then
      kill -TERM "$(cat "$OUT/pid")" 2>/dev/null
      for _ in $(seq 1 20); do kill -0 "$(cat "$OUT/pid")" 2>/dev/null || break; sleep 0.5; done
    else
      DEAD=1   # 측정기는 stop 이 TERM 을 보내기 전에 스스로 끝나지 않는다 — 이미 죽었다면 비정상이다
      echo "! 측정기가 stop 전에 이미 종료돼 있었다(비정상)"
    fi
    $PY ./measure_resources.py summarize --out "$OUT" \
        --min-samples "${EMR_MEASURE_MIN_SAMPLES:-10}" \
        --min-avail-mib "${EMR_MEASURE_MIN_AVAIL_MIB:-1500}" --max-swap-mib "${EMR_MEASURE_MAX_SWAP_MIB:-100}" \
        --max-psi-full "${EMR_MEASURE_MAX_PSI_FULL:-1.0}" --max-psi-some "${EMR_MEASURE_MAX_PSI_SOME:-10.0}" \
        --iops-cap "${EMR_MEASURE_IOPS_CAP:-3000}" --max-iops-frac "${EMR_MEASURE_MAX_IOPS_FRAC:-0.8}" \
        --max-vram-frac "${EMR_MEASURE_MAX_VRAM_FRAC:-0.90}"
    RC=$?
    if [ "$DEAD" = 1 ] || grep -q Traceback "$OUT/sampler.log" 2>/dev/null; then
      echo "MEASURE_RESULT=error  (측정기가 비정상 종료했다 — 이 측정은 신뢰할 수 없다. $OUT/sampler.log 참고)"
      RC=4
    fi
    exit $RC ;;
  *) echo "사용법: $0 start [라벨] | mark 이름 | stop"; exit 2 ;;
esac
