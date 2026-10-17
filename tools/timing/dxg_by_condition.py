#!/usr/bin/env python
"""dmesg 호스트(WDDM) 할당 거부를 조건 구간별로 집계한다(사후 대조).
dmesg 타임스탬프(부팅 후 초) → 시계 시각은 /proc/uptime 으로 환산하므로 ±1~2초 오차가 있다.
조건 구간은 run.log 의 '===== 조건 X' 시작과 '정리 확인[X]'/워치독 종료/수동 종료 시각으로 잡는다.
사용법: python dxg_by_condition.py <결과 폴더(driver.log 가 있는 곳)>
"""
import csv
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

root = Path(sys.argv[1])
t5, t6 = root / 'table5', root / 'table6'
up = float(open('/proc/uptime').read().split()[0])
now = datetime.now()
day = now.date()


def hms(s):
    return datetime.combine(day, datetime.strptime(s, '%H:%M:%S').time())


events = []
for l in subprocess.run(['dmesg'], capture_output=True, text=True).stdout.splitlines():
    m = re.match(r'\[\s*([\d.]+)\]', l)
    if m and 'dxgk' in l and 'failed' in l.lower():
        kind = 'make_resident' if 'make_resident' in l else 'create_allocation' if 'create_allocation' in l else 'other'
        events.append((now - timedelta(seconds=up - float(m.group(1))), kind, l))

# 구간
win = {}
log5 = (t5 / 'run.log').read_text(encoding='utf-8').splitlines()
start = {}
end = {}
for l in log5:
    m = re.match(r'\[(\d\d:\d\d:\d\d)\] (.*)', l)
    if not m:
        continue
    t, msg = hms(m.group(1)), m.group(2)
    mm = re.match(r'===== 조건 (\S+):', msg)
    if mm:
        start[mm.group(1)] = t
    mm = re.match(r'\s*정리 확인\[(\S+?)\]', msg)
    if mm:
        end[mm.group(1)] = t
    if 'WATCHDOG' in msg:
        for c in reversed(list(start)):
            if c not in end:
                end[c] = t
                break
rows = list(csv.DictReader((t5 / 'raw.csv').open(encoding='utf-8')))
g1 = next((r for r in rows if r['condition'] == 'g1' and r['metric'] == 'run_result'), None)
if g1 and 'g1' in start and 'g1' not in end:
    end['g1'] = start['g1'] + timedelta(seconds=float(g1['value']) + 3)
for c in start:
    win[c] = (start[c] - timedelta(seconds=2), end.get(c, start[c]) + timedelta(seconds=2))
# 표 6 / 조건 a(SAM3D 로드 포함)의 바깥 구간
drv = [l for l in (root / 'driver.log').read_text(encoding='utf-8').splitlines()]
def drv_t(tag):
    for l in drv:
        if tag in l:
            return hms(re.match(r'\[(\d\d:\d\d:\d\d)\]', l).group(1))
win['표6'] = (drv_t('PHASE 1'), drv_t('PHASE 2'))
order = ['표6'] + [c for c in ['a', 'b', 'c1', 'c2', 'd', 'e', 'f1', 'f2', 'g1', 'g2', 'b_end'] if c in win]

out_rows, lines = [], ['| 구간 | 시작 | 끝 | make_resident -12 | create_allocation -75 | 이벤트 시각 |', '|---|---|---|---|---|---|']
for c in order:
    s, e = win[c]
    ev = [x for x in events if s <= x[0] <= e]
    mr = [x for x in ev if x[1] == 'make_resident']
    ca = [x for x in ev if x[1] == 'create_allocation']
    lines.append(f'| {c} | {s:%H:%M:%S} | {e:%H:%M:%S} | {len(mr)} | {len(ca)} | {", ".join(f"{x[0]:%H:%M:%S}" for x in mr) or "—"} |')
    out_rows.append((c, len(mr), len(ca), ', '.join(f'{x[0]:%H:%M:%S}' for x in mr)))
outside = [x for x in events if not any(win[c][0] <= x[0] <= win[c][1] for c in order)]
lines += ['', f'조건·표6 구간 밖의 이벤트 {len(outside)}건: ' + ', '.join(f'{x[0]:%H:%M:%S}({x[1]})' for x in outside)]
(root / 'dxg_by_condition.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
print('\n'.join(lines))
# raw.csv 에 사후 대조 행으로 남긴다(중복 방지)
have = {(r['condition'], r['metric']) for r in rows}
with (t5 / 'raw.csv').open('a', newline='', encoding='utf-8') as f:
    w = csv.writer(f)
    for c, mr, ca, ts in out_rows:
        if c != '표6' and (c, 'dxg_make_resident_posthoc') not in have:
            w.writerow([c, 0, 'setup', 'dxg_make_resident_posthoc', mr, 'ok', f'사후 대조(dmesg 타임스탬프↔조건 구간, ±2초): {ts or "없음"}'])
