#!/usr/bin/env python
"""
SAM3D 로컬 vs fal 비교.

실행 (sam3d conda 환경, GPU 필요 — 로컬 쪽을 돌리므로):
  pip install fal_client            # 이 환경에 아직 없다
  FAL_KEY=... python tools/compare_sam3d_backends.py --images DIR --out OUT

이미지: RGBA PNG(알파=마스크, 프론트가 보내는 것과 같은 형태). 카테고리는 파일명의
첫 '_' 앞(예: 시계_01.png → '시계') 또는 --categories JSON({"파일명": "카테고리"}).

한 번의 실행으로 다음을 얻는다 (비용 = 이미지 수 × --n-seeds 회 fal 호출 + 콜드 테스트 호출):
  1. 로컬 vs fal: extent / sam3d_size_m 후보 / 방향(24개 회전 중 최적) / 소요 시간
  2. fal seed 일관성: seed 만 바꾼 N회의 extent·두께 비율·정점 수 편차
  3. 콜드스타트: --cold-intervals 간격으로 같은 이미지를 다시 호출한 지연시간 (--tests cold)
  4. 핵심 비교: 로컬(seed 5개 탐색 후 최선) 1건 vs fal seed 고정 N건 — 입체감 기준으로
     로컬과 비슷한 fal 결과가 몇 개인지, 이미지별 GLB 저장

결과는 OUT/report.md, OUT/results.json, OUT/<이미지>/{local,fal_seedN}.glb 로 남는다.
"""
import argparse
import base64
import io
import json
import itertools
import os
import sys
import time
import statistics as st

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(os.path.dirname(HERE), 'sam3d', 'backend')
sys.path.insert(0, BACKEND)

FAL_SEEDS = [101, 202, 303, 404, 505, 606, 707, 808]


# ── 메시 / 지표 ─────────────────────────────────────────────────────────

def payload_to_trimesh(payload):
    import trimesh
    m = payload['mesh']
    v = np.asarray(m['vertices'], dtype=np.float32)
    f = np.asarray(m['faces'], dtype=np.int64)
    if payload['type'] == 'textured':
        tex = Image.open(io.BytesIO(base64.b64decode(m['textureB64']))).convert('RGB')
        vis = trimesh.visual.TextureVisuals(
            uv=np.asarray(m['uvs'], dtype=np.float32),
            material=trimesh.visual.material.PBRMaterial(baseColorTexture=tex))
        return trimesh.Trimesh(vertices=v, faces=f, visual=vis, process=False)
    c = (np.clip(np.asarray(m['colors'], dtype=np.float32), 0, 1) * 255).astype(np.uint8)
    return trimesh.Trimesh(vertices=v, faces=f, vertex_colors=c, process=False)


def texture_metrics(payload, ref_rgb_mean):
    """텍스처 품질 대용 지표: 해상도, 선명도(라플라시안 분산), 입력 이미지 평균색과의 거리."""
    m = payload['mesh']
    if payload['type'] == 'textured':
        img = Image.open(io.BytesIO(base64.b64decode(m['textureB64']))).convert('RGB')
        a = np.asarray(img, dtype=np.float32)
        g = a.mean(axis=2)
        lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:])
        # 검은 여백(미사용 UV 영역)은 평균색에서 뺀다
        used = a[a.sum(axis=2) > 30]
        mean = used.mean(axis=0) if len(used) else a.reshape(-1, 3).mean(axis=0)
        return {'tex': f'{img.width}x{img.height}', 'sharp': float(lap.var()),
                'color_delta': float(np.linalg.norm(mean - ref_rgb_mean))}
    c = np.asarray(m['colors'], dtype=np.float32) * 255
    return {'tex': 'vertex_color', 'sharp': float('nan'),
            'color_delta': float(np.linalg.norm(c.mean(axis=0) - ref_rgb_mean))}


def rotations24():
    """부호 있는 축 치환 중 det=+1 인 24개 (반사는 제외 — 거울상은 다른 형상이다)."""
    out = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product([1, -1], repeat=3):
            R = np.zeros((3, 3))
            for i, (j, s) in enumerate(zip(perm, signs)):
                R[i, j] = s
            if np.linalg.det(R) > 0:
                out.append(R)
    return out


def _norm_cloud(v, n=3000, seed=0):
    rng = np.random.default_rng(seed)
    if len(v) > n:
        v = v[rng.choice(len(v), n, replace=False)]
    v = v - (v.max(axis=0) + v.min(axis=0)) / 2
    return v / max((v.max(axis=0) - v.min(axis=0)).max(), 1e-9)


def best_alignment(v_ref, v_other):
    """v_other 를 24개 회전으로 돌려 v_ref 와 chamfer 거리가 최소인 것을 찾는다."""
    from scipy.spatial import cKDTree
    a = _norm_cloud(np.asarray(v_ref))
    b = _norm_cloud(np.asarray(v_other))
    ta = cKDTree(a)

    def cham(bb):
        return float(ta.query(bb)[0].mean() + cKDTree(bb).query(a)[0].mean())

    ident = cham(b)
    best_R, best_d = np.eye(3), ident
    for R in rotations24():
        d = cham(b @ R.T)
        if d < best_d:
            best_R, best_d = R, d
    return best_R, best_d, ident


def metrics(payload):
    from services.sam3d_fal import mesh_metrics
    return mesh_metrics(payload)


def sorted_ext(ext):
    return sorted(ext, reverse=True)


# ── 입력 ────────────────────────────────────────────────────────────────

def load_images(args):
    cats = {}
    if args.categories:
        cats = json.load(open(args.categories, encoding='utf-8'))
    files = sorted(f for f in os.listdir(args.images) if f.lower().endswith('.png'))
    if args.only:
        files = [f for f in files if any(o in f for o in args.only)]
    items = []
    for f in files:
        path = os.path.join(args.images, f)
        im = Image.open(path)
        if im.mode != 'RGBA' or np.asarray(im.getchannel('A')).min() == 255:
            print(f'[건너뜀] {f}: 알파 채널(마스크)이 없다 — RGBA PNG 여야 한다')
            continue
        cat = cats.get(f) or cats.get(os.path.splitext(f)[0]) or f.split('_')[0]
        a = np.asarray(im.convert('RGBA'))
        ref = a[..., :3][a[..., 3] > 127].mean(axis=0)
        items.append({'name': os.path.splitext(f)[0], 'path': path, 'category': cat,
                      'bytes': open(path, 'rb').read(), 'ref_rgb': ref})
    return items[:args.max_images] if args.max_images else items


# ── 실행기 ──────────────────────────────────────────────────────────────

def run_local(item, pipeline, outdir, reuse):
    from services import sam3d_runner
    cache = os.path.join(outdir, 'local.json')
    if reuse and os.path.isfile(cache):
        d = json.load(open(cache))
        return d['payload'], d['seconds']
    t = time.time()
    payload = sam3d_runner.generate(pipeline, item['bytes'], item['category'])
    sec = time.time() - t
    json.dump({'payload': payload, 'seconds': sec}, open(cache, 'w'))
    return payload, sec


def run_fal_seeds(item, seeds, outdir):
    from services import sam3d_fal
    image_uri, mask_uri = sam3d_fal.prepare_inputs(item['bytes'])
    results = []
    for s in seeds:
        try:
            raw = sam3d_fal.fal_generate_raw(item['bytes'], s, image_uri, mask_uri)
            payload = sam3d_fal.convert_glb(raw, item['category'])
        except Exception as e:  # 한 건 실패가 전체 비교를 죽이지 않게
            print(f"  [fal seed={s}] 실패: {e}")
            results.append({'seed': s, 'error': str(e)})
            continue
        open(os.path.join(outdir, f'fal_seed{s}_raw.glb'), 'wb').write(raw.glb)
        payload_to_trimesh(payload).export(os.path.join(outdir, f'fal_seed{s}.glb'))
        results.append({'seed': s, 'payload': payload, 'raw': raw})
        print(f"  [fal seed={s}] {raw.t_total:.1f}초 (대기 {raw.t_queue:.1f}/실행 {raw.t_infer:.1f}) "
              f"v={len(payload['mesh']['vertices'])}")
    return results


# ── 분석 ────────────────────────────────────────────────────────────────

def analyze_item(item, local, local_sec, fal_results, tol):
    lm = metrics(local)
    ltex = texture_metrics(local, item['ref_rgb'])
    row = {'name': item['name'], 'category': item['category'], 'local': {**lm, **ltex, 'seconds': local_sec,
           'sam3d_size_m': local.get('sam3d_size_m')}, 'fal': []}
    from services.sam3d_fal import _scale_vec, axis_spec_from_matrix, check_quality
    lv = np.asarray(local['mesh']['vertices'])
    for r in fal_results:
        if 'error' in r:
            row['fal'].append({'seed': r['seed'], 'error': r['error']})
            continue
        p, raw = r['payload'], r['raw']
        fm = metrics(p)
        ftex = texture_metrics(p, item['ref_rgb'])
        R, d_best, d_ident = best_alignment(lv, np.asarray(p['mesh']['vertices']))
        sv = _scale_vec(raw.metadata)
        ext = np.asarray(fm['extent'])
        # sam3d_size_m 후보: 로컬은 (canonical extent × 스칼라 scale). fal 쪽 후보 둘을 로컬 값과 대조.
        cand = {'glb_extent': ext.tolist(),
                'extent_x_scale': (ext * sv).tolist() if sv is not None else None}
        ok, why = check_quality(p, item['category'])
        row['fal'].append({
            'seed': r['seed'], **fm, **ftex,
            'seconds': raw.t_total, 'queue': raw.t_queue, 'infer': raw.t_infer,
            'metadata_scale': None if sv is None else sv.tolist(),
            'metadata_keys': sorted(raw.metadata[0].keys()) if raw.metadata else [],
            'size_candidates': cand,
            'axis_best': axis_spec_from_matrix(R), 'axis_chamfer': d_best, 'axis_chamfer_identity': d_ident,
            'gate_ok': ok, 'gate_why': why,
            'similar_to_local': fm['thickness_ratio'] >= lm['thickness_ratio'] * (1 - tol),
        })
    return row


def consistency(fal_rows):
    ok = [r for r in fal_rows if 'error' not in r]
    if len(ok) < 2:
        return None
    ext = np.array([sorted_ext(r['extent']) for r in ok])
    cv = (ext.std(axis=0) / np.maximum(ext.mean(axis=0), 1e-9)).tolist()
    tr = [r['thickness_ratio'] for r in ok]
    nv = [r['n_vertices'] for r in ok]
    return {'n': len(ok), 'extent_cv_sorted': cv, 'thickness_mean': st.mean(tr), 'thickness_std': st.pstdev(tr),
            'verts_cv': st.pstdev(nv) / max(st.mean(nv), 1e-9),
            'axis_modes': sorted({r['axis_best'] for r in ok})}


def cold_test(item, intervals, seed):
    """간격을 두고 같은 요청을 반복해 대기 시간(t_queue)과 전체 시간을 본다."""
    from services import sam3d_fal
    image_uri, mask_uri = sam3d_fal.prepare_inputs(item['bytes'])
    rows = []
    for i, wait in enumerate(intervals):
        if wait:
            print(f'  [cold] {wait}초 대기...')
            time.sleep(wait)
        try:
            raw = sam3d_fal.fal_generate_raw(item['bytes'], seed, image_uri, mask_uri)
            rows.append({'after_idle_s': wait, 'total': raw.t_total, 'queue': raw.t_queue, 'infer': raw.t_infer})
            print(f"  [cold] 휴지 {wait}s 뒤: 총 {raw.t_total:.1f}초 (대기 {raw.t_queue:.1f}/실행 {raw.t_infer:.1f})")
        except Exception as e:
            rows.append({'after_idle_s': wait, 'error': str(e)})
    return rows


# ── 리포트 ──────────────────────────────────────────────────────────────

def fmt_ext(e):
    return '×'.join(f'{x:.3f}' for x in e)


def write_report(path, rows, cons, cold, args):
    L = []
    L.append(f'# SAM3D local vs fal 비교\n\n- 이미지 {len(rows)}장, fal seed {args.n_seeds}개, 유사 기준: 두께 비율 ≥ 로컬×(1−{args.tol})\n'
             f'- 두께 비율 = 최소 extent / 최대 extent (납작/얇은 카테고리는 부풀리기 후 값이라 비교 의미가 약하다)\n')

    L.append('\n## 1. 로컬 vs fal (이미지별)\n')
    L.append('| 이미지 | 카테고리 | 구분 | 정점 | 면 | extent(xyz) | 두께비 | 텍스처 | 선명도 | 색차 | 시간(s) |\n|---|---|---|---|---|---|---|---|---|---|---|')
    for r in rows:
        l = r['local']
        L.append(f"| {r['name']} | {r['category']} | **로컬** | {l['n_vertices']} | {l['n_faces']} | {fmt_ext(l['extent'])} | "
                 f"{l['thickness_ratio']:.3f} | {l['tex']} | {l['sharp']:.1f} | {l['color_delta']:.1f} | {l['seconds']:.1f} |")
        for f in r['fal']:
            if 'error' in f:
                L.append(f"| | | fal s{f['seed']} | 실패: {f['error'][:60]} | | | | | | | |")
                continue
            L.append(f"| | | fal s{f['seed']} | {f['n_vertices']} | {f['n_faces']} | {fmt_ext(f['extent'])} | "
                     f"{f['thickness_ratio']:.3f}{' ✓' if f['similar_to_local'] else ''} | {f['tex']} | {f['sharp']:.1f} | "
                     f"{f['color_delta']:.1f} | {f['seconds']:.1f} |")

    L.append('\n## 2. 유사 개수 요약 (fal N개 중 로컬과 입체감이 비슷한 수)\n')
    L.append('| 이미지 | 카테고리 | 유사 | 품질 게이트 통과 | fal 두께비 평균±표준편차 | 로컬 두께비 |\n|---|---|---|---|---|---|')
    tot_sim = tot_n = tot_gate = 0
    for r in rows:
        ok = [f for f in r['fal'] if 'error' not in f]
        sim = sum(f['similar_to_local'] for f in ok)
        gate = sum(f['gate_ok'] for f in ok)
        tot_sim += sim; tot_n += len(ok); tot_gate += gate
        c = cons.get(r['name'])
        ms = f"{c['thickness_mean']:.3f}±{c['thickness_std']:.3f}" if c else '-'
        L.append(f"| {r['name']} | {r['category']} | {sim}/{len(ok)} | {gate}/{len(ok)} | {ms} | {r['local']['thickness_ratio']:.3f} |")
    if tot_n:
        L.append(f'\n**전체: {tot_sim}/{tot_n} ({tot_sim / tot_n * 100:.0f}%) 가 로컬 수준, 품질 게이트 통과 {tot_gate}/{tot_n} ({tot_gate / tot_n * 100:.0f}%)**\n')

    L.append('\n## 3. seed 일관성 (fal끼리)\n')
    L.append('| 이미지 | 정렬된 extent CV(큰→작은) | 두께비 표준편차 | 정점 수 CV |\n|---|---|---|---|')
    for r in rows:
        c = cons.get(r['name'])
        if c:
            L.append(f"| {r['name']} | {', '.join(f'{x:.3f}' for x in c['extent_cv_sorted'])} | {c['thickness_std']:.3f} | {c['verts_cv']:.3f} |")

    L.append('\n## 4. 스케일(E)·방향(F) 실측\n')
    L.append('sam3d_size_m 는 로컬 값(canonical extent × 스칼라 scale)과 fal 후보 두 가지를 정렬된 크기(큰→작은)로 대조한다. 방향은 24개 회전 중 chamfer 최소.\n')
    L.append('| 이미지 | 로컬 size_m(정렬) | fal glb_extent(정렬) | fal extent×scale(정렬) | fal metadata scale | 최적 축 보정 | chamfer 보정후/전 |\n|---|---|---|---|---|---|---|')
    axis_votes = {}
    for r in rows:
        ls = r['local'].get('sam3d_size_m')
        lsv = fmt_ext(sorted_ext([ls['width'], ls['height'], ls['depth']])) if ls else '없음'
        for f in r['fal'][:1]:
            if 'error' in f:
                continue
            c = f['size_candidates']
            ge = fmt_ext(sorted_ext(c['glb_extent']))
            es = fmt_ext(sorted_ext(c['extent_x_scale'])) if c['extent_x_scale'] else '없음'
            L.append(f"| {r['name']} | {lsv} | {ge} | {es} | {f['metadata_scale']} | `{f['axis_best']}` | {f['axis_chamfer']:.3f}/{f['axis_chamfer_identity']:.3f} |")
        for f in r['fal']:
            if 'error' not in f:
                axis_votes[f['axis_best']] = axis_votes.get(f['axis_best'], 0) + 1
    if axis_votes:
        L.append('\n축 보정 득표: ' + ', '.join(f'`{k}` ×{v}' for k, v in sorted(axis_votes.items(), key=lambda x: -x[1])))
        L.append('\n(`x,y,z` 가 압도적이면 보정 불필요. 다르면 SAM3D_FAL_AXIS 후보. 단, 로컬 쪽은 프론트에서 Y 를 뒤집어 쓰므로 "로컬과 같은 좌표계"가 곧 프론트 기준이다.)\n')

    if cold:
        L.append('\n## 5. 콜드스타트 / 지연\n')
        L.append('| 직전 호출 후 휴지(s) | 총(s) | 대기(s) | 실행(s) |\n|---|---|---|---|')
        for c in cold:
            if 'error' in c:
                L.append(f"| {c['after_idle_s']} | 실패: {c['error'][:60]} | | |")
            else:
                L.append(f"| {c['after_idle_s']} | {c['total']:.1f} | {c['queue']:.1f} | {c['infer']:.1f} |")

    open(path, 'w', encoding='utf-8').write('\n'.join(L) + '\n')


# ── main ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--images', required=True, help='RGBA PNG 디렉터리')
    ap.add_argument('--out', required=True)
    ap.add_argument('--categories', help='{"파일명": "카테고리"} JSON')
    ap.add_argument('--only', nargs='*', help='파일명에 이 문자열이 든 것만')
    ap.add_argument('--max-images', type=int, default=0)
    ap.add_argument('--n-seeds', type=int, default=5, help='이미지당 fal seed 수 (3~5 권장, 최대 8)')
    ap.add_argument('--tol', type=float, default=0.25, help='fal 두께비가 로컬×(1−tol) 이상이면 "비슷함"')
    ap.add_argument('--tests', default='compare', help='compare,cold (쉼표 구분)')
    ap.add_argument('--cold-intervals', default='0,30,120,300', help='콜드 테스트 휴지 간격(초). 첫 값은 보통 0')
    ap.add_argument('--skip-local', action='store_true', help='로컬 실행 생략(캐시 local.json 필요)')
    ap.add_argument('--reuse-local', action='store_true', help='OUT/<이미지>/local.json 이 있으면 재사용')
    args = ap.parse_args()
    args.n_seeds = min(args.n_seeds, len(FAL_SEEDS))
    tests = set(args.tests.split(','))

    from services import sam3d_fal
    sam3d_fal._require_key()  # 키 없으면 GPU 로드 전에 실패
    os.makedirs(args.out, exist_ok=True)
    items = load_images(args)
    if not items:
        sys.exit('처리할 이미지가 없다')
    print(f'이미지 {len(items)}장, fal 호출 예상 {len(items) * args.n_seeds}회'
          + (f' + 콜드 {len(args.cold_intervals.split(","))}회' if 'cold' in tests else ''))

    rows, cons, cold = [], {}, None
    if 'compare' in tests:
        pipeline = None
        if not args.skip_local:
            from services import sam3d_runner
            if not (args.reuse_local and all(os.path.isfile(os.path.join(args.out, i['name'], 'local.json')) for i in items)):
                print('로컬 파이프라인 로드...')
                pipeline = sam3d_runner.load_pipeline()
        for it in items:
            print(f"\n=== {it['name']} ({it['category']}) ===")
            od = os.path.join(args.out, it['name'])
            os.makedirs(od, exist_ok=True)
            local, lsec = run_local(it, pipeline, od, args.reuse_local or args.skip_local)
            payload_to_trimesh(local).export(os.path.join(od, 'local.glb'))
            print(f"  [local] {lsec:.1f}초 v={len(local['mesh']['vertices'])}")
            fal_res = run_fal_seeds(it, FAL_SEEDS[:args.n_seeds], od)
            row = analyze_item(it, local, lsec, fal_res, args.tol)
            rows.append(row)
            c = consistency(row['fal'])
            if c:
                cons[it['name']] = c

    if 'cold' in tests:
        print('\n=== 콜드스타트 테스트 ===')
        cold = cold_test(items[0], [int(x) for x in args.cold_intervals.split(',')], FAL_SEEDS[0])

    json.dump({'rows': rows, 'consistency': cons, 'cold': cold, 'stats': sam3d_fal.stats_snapshot()},
              open(os.path.join(args.out, 'results.json'), 'w'), ensure_ascii=False, indent=1, default=str)
    write_report(os.path.join(args.out, 'report.md'), rows, cons, cold, args)
    print(f"\n리포트: {os.path.join(args.out, 'report.md')}")


if __name__ == '__main__':
    main()
