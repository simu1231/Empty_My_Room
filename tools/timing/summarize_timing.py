#!/usr/bin/env python
"""raw.csv → table.md (장면별 평균±표준편차 + 전체 행 + 보조 표 + 이상치/실패 목록).

    python tools/timing/summarize_timing.py <raw.csv> [--repeats 5]

집계 규칙
  * phase=measure 이고 status 가 ok|warn 인 행만 평균에 넣는다. warmup 은 CSV 에만 있다.
  * ±는 표본 표준편차(n-1). 셀의 n 이 --repeats 와 다르면 (n=k) 를 붙인다.
  * 이상치는 **제외하지 않는다.** 같은 (장면, 모드, 단계, 가구) 에서 중앙값의 2배 초과/0.5배 미만이면 표시만 한다.
  * 실패(status=failed)는 평균에서 빠지고, 아래 목록에 그대로 나온다. 재실행하지 않는다.
"""
import argparse
import csv
import statistics as st
from collections import defaultdict
from pathlib import Path

SCALE_COL = '스케일 계산(Omni3D+SAM3D scale, 서버)'


def fmt(vals, n_expect):
    if not vals:
        return '—'
    m = st.mean(vals)
    s = f'{m:.2f}±{st.stdev(vals):.2f}' if len(vals) > 1 else f'{m:.2f}±n/a'
    return s + ('' if len(vals) == n_expect else f' (n={len(vals)})')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('raw')
    ap.add_argument('--repeats', type=int, default=5)
    a = ap.parse_args(argv)
    raw = Path(a.raw)
    rows = list(csv.DictReader(raw.open(encoding='utf-8')))
    for r in rows:
        r['seconds'] = float(r['seconds'])
    ok = [r for r in rows if r['phase'] == 'measure' and r['status'] in ('ok', 'warn')]
    scenes = list(dict.fromkeys(r['scene'] for r in rows if r['scene'] not in ('_global', 'preview')))

    # (scene, iteration) → {열: 값}  — 단계 열은 sequential, 전체(병렬)는 parallel 에서
    cell = defaultdict(dict)
    by = defaultdict(list)
    for r in ok:
        by[(r['scene'], int(r['iteration']), r['mode'], r['stage'])].append(r)

    def total(sc, it, mode, stage):
        v = by.get((sc, it, mode, stage))
        return sum(x['seconds'] for x in v) if v else None

    def mean_over_furniture(sc, it, stage):
        v = by.get((sc, it, 'sequential', stage))
        return st.mean(x['seconds'] for x in v) if v else None

    iters = sorted({int(r['iteration']) for r in ok})
    furn_n = {}
    for r in rows:
        if r['stage'] == 'SAM3D' and r['phase'] == 'measure':
            furn_n.setdefault(r['scene'], set()).add(r['furniture'])
    cols = ['SAM2', 'uLayout', 'LaMa', 'SD', 'SD 로드', 'SAM3D(가구당)', SCALE_COL, '전체(순차)', '전체(병렬)']
    for sc in scenes:
        for it in iters:
            c = {}
            m, e = total(sc, it, 'sequential', 'SAM2_mask'), total(sc, it, 'sequential', 'SAM2_extract')
            c['SAM2'] = m + e if m is not None and e is not None else None
            i, rc = total(sc, it, 'sequential', 'uLayout_infer'), total(sc, it, 'sequential', 'uLayout_rectify')
            c['uLayout'] = i + rc if i is not None and rc is not None else None
            c['LaMa'] = total(sc, it, 'sequential', 'LaMa')
            c['SD'] = total(sc, it, 'sequential', 'SD')
            c['SD 로드'] = total(sc, it, 'sequential', 'SD_load')
            c['SAM3D(가구당)'] = mean_over_furniture(sc, it, 'SAM3D')
            c[SCALE_COL] = mean_over_furniture(sc, it, 'Scale_Omni3D')
            c['전체(순차)'] = total(sc, it, 'sequential', 'e2e')
            c['전체(병렬)'] = total(sc, it, 'parallel', 'e2e')
            cell[(sc, it)] = c

    L = ['# 처리 시간 측정 결과', '',
         f'원시 데이터: `{raw.name}` · 단위 초 · 측정 {a.repeats}회(워밍업 제외) · ±는 표본 표준편차', '',
         '## 표 1. 장면별 처리 시간', '',
         '| 장면 | 가구 수 | ' + ' | '.join(cols) + ' |', '|' + '---|' * (len(cols) + 2)]
    pooled = defaultdict(list)
    for sc in scenes:
        vals = {c: [cell[(sc, it)][c] for it in iters if cell[(sc, it)].get(c) is not None] for c in cols}
        for c in cols:
            pooled[c] += vals[c]
        L.append(f'| {sc} | {len(furn_n.get(sc, []))} | ' + ' | '.join(fmt(vals[c], a.repeats) for c in cols) + ' |')
    n_all = a.repeats * len(scenes)
    L.append(f'| **전체 평균** | {sum(len(v) for v in furn_n.values())} | '
             + ' | '.join(fmt(pooled[c], n_all) for c in cols) + ' |')
    L += ['', '전체 평균 행은 모든 장면의 측정값을 한데 모은 평균±표준편차다(장면 평균의 평균이 아니다). '
          '가구 수가 다른 장면이 섞이므로 SAM3D(가구당)와 스케일 열만 가구 단위로 정규화돼 있다.', '']

    L += ['### 열 정의', '',
          '- **SAM2** = `SAM2_mask`(클릭 전체 점으로 마스크 1개) + `SAM2_extract`(원본 사진에서 가구별 추출). 둘 다 SAM2 추론이다.',
          '- **uLayout** = `/infer` + `/rectify` HTTP 왕복 시간 합(순차). 사이드카가 PerspectiveFields 를 요청마다 로드/해제하므로 로드가 포함돼 있고 분리할 수 없다.',
          '- **SD** = 추론만. **SD 로드**는 `SDService()` 생성(`from_pretrained`)이며 요청마다 일어난다. 해제(`SD_unload`)는 표 3.',
          '- **SAM3D(가구당)** = `generate()` + 응답 직렬화(orjson). 파이프라인은 상주(로드는 표 3). 이 값은 가구별 평균이다.',
          f'- **{SCALE_COL}** = Omni3D `/estimate` HTTP 왕복. SAM3D 포즈 디코더 scale 환산(`generate()` 마지막의 numpy 몇 줄)은 '
          '코드를 수정하지 않고는 분리할 수 없어 SAM3D 열에 포함돼 있다. **Omni3D 미지원 카테고리(침대·소파·의자 등)는 프런트도 호출하지 않으므로 이 열에 들어가지 않는다.**',
          '- **프런트 JS의 카메라 포즈 기반 광선–바닥 교차는 측정에서 제외했다**(브라우저에서 실행되는 코드).',
          '- **전체(순차)** = SAM2_mask 부터 마지막 가구의 SAM3D/Omni3D 까지의 wall-clock. 단계 합이 아니라 따로 잰 값이며 SD 로드/해제를 포함한다. '
          'SAM2/LaMa 의 세션 시작 로드(`load_*`)와 사용자 클릭 시간은 제외.',
          '- **전체(병렬)** = 같은 흐름에서 uLayout `/infer`‖`/rectify` 와 가구별 SAM3D‖Omni3D 를 동시에 보낸 wall-clock.', '']

    # 표 2. 가구별
    L += ['## 표 2. 가구별 SAM3D / 스케일 / 병렬 구간', '',
          '| 장면 | 가구 | SAM3D(순차) | Omni3D(순차) | 순차 합 | 병렬 구간 wall-clock |', '|---|---|---|---|---|---|']
    for sc in scenes:
        for fn in sorted(furn_n.get(sc, [])):
            def per_it(mode, stage):
                d = defaultdict(float)
                for r in ok:
                    if r['scene'] == sc and r['mode'] == mode and r['stage'] == stage and r['furniture'] == fn:
                        d[int(r['iteration'])] += r['seconds']
                return [d[i] for i in sorted(d)]
            s3, sc_, par = per_it('sequential', 'SAM3D'), per_it('sequential', 'Scale_Omni3D'), per_it('parallel', 'furniture_phase')
            both = [x + y for x, y in zip(s3, sc_)] if sc_ and len(sc_) == len(s3) else s3
            L.append(f'| {sc} | {fn} | {fmt(s3, a.repeats)} | {fmt(sc_, a.repeats)} | {fmt(both, a.repeats)} | {fmt(par, a.repeats)} |')
    L.append('')

    # 표 3. 로드/해제
    L += ['## 표 3. 모델 로딩·해제 (추론 시간과 분리)', '', '| 항목 | 값(초) | 비고 |', '|---|---|---|']
    for r in rows:
        if r['stage'] == 'load_SAM3D':
            L.append(f'| SAM3D 파이프라인 로드 | {r["seconds"]:.2f} | 프로세스당 1회(라우터는 app.state 캐시, 워커는 프로세스 수명) |')
    for stage, note in (('SD_load', '요청마다 로드(inpaint.py)'), ('SD_unload', '요청마다 해제'),
                        ('load_SAM2', '세션 시작 시 재로드 — 앞 실행이 release 했기 때문'),
                        ('load_LaMa', '위와 같음'), ('release_SAM2_LaMa', '명시적 해제 호출')):
        v = [r['seconds'] for r in ok if r['stage'] == stage and r['mode'] == 'sequential']
        L.append(f'| {stage} | {fmt(v, n_all)} | {note} |')
    L.append('')

    # 표 4. 병렬 구간 vs 순차
    L += ['## 표 4. 병렬 구간 (장면 합계)', '', '| 장면 | uLayout 순차(infer+rectify) | uLayout 병렬 구간 | 가구 구간 순차 합(SAM3D+Omni3D) | 가구 구간 병렬 합 |', '|---|---|---|---|---|']
    for sc in scenes:
        def per_it_sum(mode, stages):
            d = defaultdict(float)
            for r in ok:
                if r['scene'] == sc and r['mode'] == mode and r['stage'] in stages:
                    d[int(r['iteration'])] += r['seconds']
            return [d[i] for i in sorted(d)]
        L.append(f'| {sc} | {fmt(per_it_sum("sequential", {"uLayout_infer", "uLayout_rectify"}), a.repeats)} '
                 f'| {fmt(per_it_sum("parallel", {"uLayout_phase"}), a.repeats)} '
                 f'| {fmt(per_it_sum("sequential", {"SAM3D", "Scale_Omni3D"}), a.repeats)} '
                 f'| {fmt(per_it_sum("parallel", {"furniture_phase"}), a.repeats)} |')
    L.append('')

    # 이상치 / 실패 / 경고
    groups = defaultdict(list)
    for r in ok:
        if r['stage'] not in ('stage_sum',):
            groups[(r['scene'], r['mode'], r['stage'], r['furniture'])].append(r)
    out_rows = []
    for k, v in groups.items():
        if len(v) < 3:
            continue
        med = st.median(x['seconds'] for x in v)
        for x in v:
            if med > 0.05 and (x['seconds'] > 2 * med or x['seconds'] < 0.5 * med):
                out_rows.append((k, x['iteration'], x['seconds'], med))
    L += ['## 이상치 (평균에서 제외하지 않음)', '']
    L += ['| 장면 | 모드 | 단계 | 가구 | 반복 | 값 | 중앙값 |', '|---|---|---|---|---|---|---|'] if out_rows else ['없음']
    for (sc, mode, stage, fn), it, sec, med in out_rows:
        L.append(f'| {sc} | {mode} | {stage} | {fn} | {it} | {sec:.2f} | {med:.2f} |')
    fails = [r for r in rows if r['status'] == 'failed']
    L += ['', f'## 실패 ({len(fails)}행) — 재실행하지 않았다', '']
    if fails:
        L += ['| 장면 | 반복 | phase | 모드 | 단계 | 가구 | 메시지 |', '|---|---|---|---|---|---|---|']
        L += [f'| {r["scene"]} | {r["iteration"]} | {r["phase"]} | {r["mode"]} | {r["stage"]} | {r["furniture"]} | {r["note"]} |' for r in fails]
    else:
        L.append('없음')
    warns = [r for r in rows if r['status'] == 'warn']
    L += ['', f'## 경고 ({len(warns)}행) — HTTP 는 성공했지만 응답이 success=false', '']
    if warns:
        L += [f'- {r["scene"]} it={r["iteration"]} {r["phase"]} {r["mode"]} {r["stage"]} {r["furniture"]}: {r["note"]}' for r in warns]
    else:
        L.append('없음')
    (raw.parent / 'table.md').write_text('\n'.join(L) + '\n', encoding='utf-8')
    print('\n'.join(L))


if __name__ == '__main__':
    main()
