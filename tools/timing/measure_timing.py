#!/usr/bin/env python
"""처리 시간 측정 (논문 VI장 표용). 기존 파이프라인 코드는 수정하지 않고 import 해서 쓴다.

측정 방식(하이브리드)
  직접 import : SAM2 / LaMa / SD(ControlNet) / SAM3D   (sam3d/backend/services/*)
  HTTP        : uLayout(:8002) / Omni3D(:8003)          (사이드카, 별도 프로세스)

실제 흐름을 그대로 따른다(프런트 → 라우터 순서).
  SAM2_mask → LaMa → SD → SAM2_extract → SAM2/LaMa 해제 → uLayout → (SAM3D, Omni3D)/가구
mode=sequential : 단계 열 측정용. 모든 호출을 순차로 돌린다.
mode=parallel   : 프런트처럼 동시에 보낸다. 병렬 구간은 두 곳이다.
                    1) uLayout /infer ‖ /rectify          (RoomMakingStep.jsx)
                    2) SAM3D ‖ Omni3D, 가구마다           (Interior3DStep.jsx omni3dPromise)

주의: 라우터 안에 있는 로직(extract 의 마스크 후처리, inpaint 의 SD 온디맨드 로드/해제)은
함수로 떼어져 있지 않아 여기서 같은 순서로 재현했다. 라우터를 고치면 이 파일도 맞춰야 한다.
"""
import argparse
import csv
import gc
import io
import json
import os
import statistics
import sys
import threading
import time
import traceback
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / 'sam3d' / 'backend'

# backend/main.py 와 같은 환경변수. sam3d_objects 를 import 하기 전에 있어야 한다.
os.environ.setdefault('LIDRA_SKIP_INIT', 'true')
os.environ.setdefault('CUDA_HOME', os.environ.get('CONDA_PREFIX', ''))
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 프런트(Interior3DStep.jsx OMNI3D_SUPPORTED)와 같은 목록. 여기 없는 가구는 Omni3D 를 부르지 않는다.
OMNI3D_SUPPORTED = ['스탠드 조명', '조명', '액자', '그림', '거울', '커튼']
# extract.py 의 THIN_FLAT 과 같은 목록
THIN_FLAT = {'조명', '시계', '화분', '꽃', '액자', '그림', '거울'}
MAX_SIZE = 1024

SUM_STAGES = {'SAM2_mask', 'LaMa', 'SD_load', 'SD', 'SD_unload', 'SAM2_extract',
              'release_SAM2_LaMa', 'uLayout_infer', 'uLayout_rectify', 'SAM3D', 'Scale_Omni3D'}


@dataclass
class Ctx:
    scene: str
    it: int
    phase: str   # warmup | measure
    mode: str    # sequential | parallel | _global


class Recorder:
    """CSV 를 한 줄씩 flush 한다 — 도중에 죽어도 거기까지는 남는다."""

    def __init__(self, path):
        self._f = open(path, 'w', newline='', encoding='utf-8')
        self._w = csv.writer(self._f)
        self._w.writerow(['scene', 'iteration', 'phase', 'mode', 'stage', 'furniture',
                          'seconds', 'status', 'note'])
        self._lock = threading.Lock()
        self.rows = []

    def add(self, ctx, stage, sec, furniture='', status='ok', note=''):
        row = [ctx.scene, ctx.it, ctx.phase, ctx.mode, stage, furniture, f'{sec:.4f}', status, note]
        with self._lock:
            self._w.writerow(row)
            self._f.flush()
            self.rows.append(row)

    def stage_sum(self, ctx):
        with self._lock:
            return sum(float(r[6]) for r in self.rows
                       if r[0] == ctx.scene and r[1] == ctx.it and r[3] == ctx.mode
                       and r[4] in SUM_STAGES and r[7] in ('ok', 'warn'))


def _sync(torch):
    try:
        if torch.cuda.is_available():
            torch.cuda.synchronize()
    except Exception:
        pass


@contextmanager
def timed(S, ctx, stage, furniture='', gpu=True, note=''):
    """perf_counter 로 잰다. GPU 단계는 전후에 cuda.synchronize(). 실패도 행으로 남기고 다시 던진다."""
    h = {'note': note, 'status': 'ok'}
    if gpu:
        _sync(S.torch)
    t0 = time.perf_counter()
    try:
        yield h
    except BaseException as e:
        if gpu:
            _sync(S.torch)
        S.rec.add(ctx, stage, time.perf_counter() - t0, furniture, 'failed', f'{type(e).__name__}: {e}'[:300])
        raise
    if gpu:
        _sync(S.torch)
    S.rec.add(ctx, stage, time.perf_counter() - t0, furniture, h['status'], h['note'])


class State:
    pass


def log(S, msg):
    line = f'[{datetime.now().strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    S.logf.write(line + '\n')
    S.logf.flush()


# ── 이미지 입출력 ────────────────────────────────────────────────────────────
def load_resized(path, exif=True):
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    pil = Image.open(path).convert('RGB')
    if exif:   # segment.py 는 exif_transpose 를 하고 extract.py 는 하지 않는다
        pil = ImageOps.exif_transpose(pil)
    img = np.array(pil)
    h, w = img.shape[:2]
    scale = 1.0
    if max(h, w) > MAX_SIZE:
        scale = MAX_SIZE / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    return img, scale


def save_overlay(path, image_np, mask, points, scale):
    import cv2
    import numpy as np
    ov = image_np.copy()
    m = mask > 127
    ov[m] = (0.5 * ov[m] + 0.5 * np.array([255, 0, 0])).astype(np.uint8)
    for x, y in points:
        c = (int(x * scale), int(y * scale))
        cv2.circle(ov, c, 9, (255, 255, 255), -1)
        cv2.circle(ov, c, 6, (0, 200, 0), -1)
    cv2.imwrite(str(path), ov[:, :, ::-1])


# ── extract.py 재현 ─────────────────────────────────────────────────────────
def extract_all(S, scene, masks_dir=None):
    """routers/extract.py 의 가구 추출 루프. [{name, png, bbox_orig, points, mask}] 를 돌려준다."""
    import cv2
    import numpy as np
    sam2 = S.res.ensure('sam2')
    image_np, scale = load_resized(REPO / scene['path'], exif=False)
    items = []
    sam2.predictor.set_image(image_np)
    for f in scene['furniture']:
        pts = [[int(p[0] * scale), int(p[1] * scale)] for p in f['points']]
        masks, scores, _ = sam2.predictor.predict(
            point_coords=np.array(pts), point_labels=np.array([1] * len(pts)), multimask_output=True)
        areas = [np.sum(m) for m in masks]
        best = masks[int(np.argmax(areas))].astype(bool)
        mask_raw = (best * 255).astype(np.uint8)
        k = 5 if any(t in f['name'] for t in THIN_FLAT) else 25
        mask_raw = cv2.morphologyEx(mask_raw, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
        contours, _ = cv2.findContours(mask_raw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(mask_raw, contours, -1, 255, thickness=cv2.FILLED)
        refined = S.extract.refine_mask(mask_raw, edge_blur=3)
        img, bbox = S.extract.extract_furniture(image_np, refined)
        if img is None:
            raise RuntimeError(f'{scene["id"]}/{f["name"]}: 마스크가 비어 있음 (좌표 확인 필요)')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        items.append({'name': f['name'], 'png': buf.getvalue(), 'points': f['points'],
                      'bbox_orig': [round(x / scale, 1) for x in bbox],   # (rmin, rmax, cmin, cmax)
                      'mask': refined, 'scale': scale, 'image': image_np})
    return items


def dump_overlays(items, scene, masks_dir):
    masks_dir.mkdir(parents=True, exist_ok=True)
    for it in items:
        area = (it['mask'] > 127).mean() * 100
        save_overlay(masks_dir / f'{scene["id"]}_{it["name"]}.png', it['image'], it['mask'],
                     it['points'], it['scale'])
        yield it['name'], area


# ── HTTP 사이드카 ───────────────────────────────────────────────────────────
def _post(url, data_bytes, filename, ctype, data=None, timeout=300):
    import requests
    r = requests.post(url, files={'image': (filename, data_bytes, ctype)}, data=data or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _ctype(path):
    return 'image/png' if str(path).lower().endswith('.png') else 'image/jpeg'


def call_omni3d(S, scene, item):
    y0, y1, x0, x1 = item['bbox_orig']
    cat = next(k for k in OMNI3D_SUPPORTED if k in item['name'])
    p = REPO / scene['path']
    return _post(f'{S.omni_url}/estimate', p.read_bytes(), p.name, _ctype(p),
                 data={'bbox': f'{x0},{y0},{x1},{y1}', 'category': cat}, timeout=120)


def call_ulayout(S, endpoint, jpg):
    return _post(f'{S.ulayout_url}/{endpoint}', jpg, 'room.jpg', 'image/jpeg', timeout=180)


# ── 한 번의 실행 ────────────────────────────────────────────────────────────
def run_once(S, scene, ctx, masks_dir=None):
    import numpy as np
    from PIL import Image
    import orjson
    from services.sd_service import SDService
    torch = S.torch
    rec = S.rec
    seq = ctx.mode == 'sequential'

    # 세션 시작 시 모델 로드. 앞 실행이 끝에서 SAM2/LaMa 를 해제했으므로(실제 흐름과 같다) 여기서
    # 다시 올라온다. 추론/전체 시간에는 넣지 않고 load_* 로 따로 남긴다.
    with timed(S, ctx, 'load_SAM2'):
        S.res.ensure('sam2')
    with timed(S, ctx, 'load_LaMa'):
        S.res.ensure('lama')

    _sync(torch)
    t_e2e = time.perf_counter()
    try:
        # 1) /api/segment/mask — 사용자가 찍은 모든 점으로 마스크 1개
        all_pts = [p for f in scene['furniture'] for p in f['points']]
        with timed(S, ctx, 'SAM2_mask'):
            image_np, scale = load_resized(REPO / scene['path'], exif=True)
            pts = [[int(p[0] * scale), int(p[1] * scale)] for p in all_pts]
            with S.res.use('sam2') as sam2:
                mask = sam2.predict(image_np, pts)['mask']

        # 2) /api/inpaint/remove — LaMa
        with timed(S, ctx, 'LaMa'):
            with S.res.use('lama') as lama:
                lama_result = lama.inpaint(image_np, mask)

        # 3) SD 는 요청마다 로드하고 끝나면 해제한다(inpaint.py). 로드/추론/해제를 따로 잰다.
        holder = []
        try:
            with timed(S, ctx, 'SD_load'):
                holder.append(SDService())
            with timed(S, ctx, 'SD'):
                final = holder[0].inpaint(lama_result, mask)
        finally:
            with timed(S, ctx, 'SD_unload'):
                holder.clear()
                gc.collect()
                torch.cuda.empty_cache()

        # 4) /api/extract/furniture — 원본 사진에서 가구별 SAM2 (extract_all 안에서 ensure)
        with timed(S, ctx, 'SAM2_extract'):
            items = extract_all(S, scene)
        if masks_dir is not None:
            for name, area in dump_overlays(items, scene, masks_dir):
                log(S, f'  overlay {scene["id"]}/{name}: 마스크 면적 {area:.1f}%')

        # 5) /api/segment/release — 프런트가 2→3 단계 전이에서 부른다
        with timed(S, ctx, 'release_SAM2_LaMa'):
            S.res.release('timing')

        # 6) 3단계 — uLayout. 입력은 SD 가 만든 빈 방(프런트도 이 파일을 보낸다)
        buf = io.BytesIO()
        Image.fromarray(final).save(buf, format='JPEG', quality=92)
        jpg = buf.getvalue()

        def ul(endpoint):
            with timed(S, ctx, f'uLayout_{endpoint}', gpu=False) as h:
                r = call_ulayout(S, endpoint, jpg)
                if not r.get('success', False):
                    h['status'] = 'warn'
                    h['note'] = f'api success=false: {str(r.get("error", ""))[:120]}'
                return r

        if seq:
            ul('infer')
            ul('rectify')
        else:
            with timed(S, ctx, 'uLayout_phase', gpu=False):
                with ThreadPoolExecutor(2) as ex:
                    for f in [ex.submit(ul, 'infer'), ex.submit(ul, 'rectify')]:
                        f.result()

        # 7) 4단계 — 가구마다 SAM3D (+ Omni3D)
        for it in items:
            name = it['name']
            supported = any(k in name for k in OMNI3D_SUPPORTED)

            def do_sam3d():
                from services import sam3d_runner
                with timed(S, ctx, 'SAM3D', name) as h:
                    payload = sam3d_runner.generate(S.pipeline, it['png'], name)
                    orjson.dumps(payload)   # 라우터가 응답으로 직렬화하는 비용도 서버 시간이다
                    h['note'] = f'type={payload.get("type")}; sam3d_size_m={payload.get("sam3d_size_m") is not None}'
                    return payload

            def do_scale():
                with timed(S, ctx, 'Scale_Omni3D', name, gpu=False) as h:
                    r = call_omni3d(S, scene, it)
                    if not r.get('success', False):
                        h['status'] = 'warn'
                        h['note'] = f'api success=false: {str(r.get("error", ""))[:120]}'
                    else:
                        h['note'] = f'dims_m={json.dumps(r.get("dimensions_m"))}'
                    return r

            if seq:
                do_sam3d()
                if supported:
                    do_scale()
                else:
                    rec.add(ctx, 'Scale_Omni3D', 0.0, name, 'skipped', 'Omni3D 미지원 카테고리 — 프런트도 호출 안 함')
            else:
                with timed(S, ctx, 'furniture_phase', name):
                    with ThreadPoolExecutor(2) as ex:
                        fs = [ex.submit(do_sam3d)] + ([ex.submit(do_scale)] if supported else [])
                        for f in fs:
                            f.result()
        _sync(torch)
        e2e = time.perf_counter() - t_e2e
        rec.add(ctx, 'e2e', e2e, note='wall-clock; SD 로드/해제 포함, 모델 상주 로드(load_*) 제외')
        if seq:
            rec.add(ctx, 'stage_sum', rec.stage_sum(ctx), note='단계 합(참고용) — e2e 와의 차이는 단계 밖 작업(JPEG 인코딩 등)')
        return True
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            raise
        _sync(torch)
        rec.add(ctx, 'e2e', time.perf_counter() - t_e2e, status='failed', note=f'{type(e).__name__}: {e}'[:300])
        log(S, f'!! 실패 {ctx}: {type(e).__name__}: {e}')
        S.logf.write(traceback.format_exc() + '\n')
        S.logf.flush()
        return False
    finally:
        # 실패해도 GPU 에 남은 참조를 정리해야 다음 실행이 연쇄 OOM 으로 죽지 않는다.
        # (재실행이 아니다 — 실패한 실행은 failed 행으로 남고 다시 돌리지 않는다.)
        gc.collect()
        torch.cuda.empty_cache()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--scenes', default=str(Path(__file__).with_name('scenes.json')))
    ap.add_argument('--repeats', type=int, default=5)
    ap.add_argument('--warmup', type=int, default=1)
    ap.add_argument('--out', default=None)
    ap.add_argument('--only', default=None, help='쉼표로 구분한 장면 id 만 실행')
    ap.add_argument('--preview-only', action='store_true',
                    help='SAM2 마스크 오버레이만 저장하고 끝낸다(좌표 검증용, 시간 측정 안 함)')
    ap.add_argument('--ulayout-url', default='http://localhost:8002')
    ap.add_argument('--omni3d-url', default='http://localhost:8003')
    ap.add_argument('--allow-backend', action='store_true', help=':8001 이 떠 있어도 진행(측정이 오염됨)')
    a = ap.parse_args()

    import requests
    import torch
    import env_report

    S = State()
    S.torch = torch
    S.ulayout_url, S.omni_url = a.ulayout_url.rstrip('/'), a.omni3d_url.rstrip('/')
    out = Path(a.out) if a.out else REPO / 'eval' / 'timing_results' / datetime.now().strftime('%Y%m%d_%H%M%S')
    (out / 'masks').mkdir(parents=True, exist_ok=True)
    S.logf = open(out / 'run.log', 'a', encoding='utf-8')
    scenes = json.loads(Path(a.scenes).read_text(encoding='utf-8'))['scenes']
    if a.only:
        keep = set(a.only.split(','))
        scenes = [s for s in scenes if s['id'] in keep]
    (out / 'scenes.json').write_text(json.dumps({'scenes': scenes}, ensure_ascii=False, indent=2), encoding='utf-8')
    log(S, f'출력: {out}')
    for s in scenes:
        log(S, f'장면 {s["id"]} {s["path"]} {s["size"]} 원본파일명={s["orig_name"]}')
        for f in s['furniture']:
            log(S, f'  가구 "{f["name"]}" 클릭 좌표(원본 해상도) {f["points"]}')

    # 모델 상주 관리자. 실제 서버와 같은 클래스를 쓴다(TTL 은 끈다).
    from services.residency import SegmentResidency
    from services.extract_service import ExtractService
    S.res = SegmentResidency(types.SimpleNamespace(state=types.SimpleNamespace(sam2=None, lama=None)), ttl_sec=0)
    S.extract = ExtractService()

    if a.preview_only:
        ctx = Ctx('preview', 0, 'warmup', 'sequential')
        S.rec = Recorder(out / 'raw_preview.csv')
        S.res.ensure('sam2')
        for sc in scenes:
            items = extract_all(S, sc)
            for name, area in dump_overlays(items, sc, out / 'masks'):
                log(S, f'overlay {sc["id"]}/{name}: 마스크 면적 {area:.1f}%')
        log(S, f'오버레이 저장: {out / "masks"}')
        return

    # 사전 점검 — 측정 조건이 깨져 있으면 시작하지 않는다
    import subprocess
    if subprocess.run("ss -ltn | grep -q ':8001 '", shell=True).returncode == 0 and not a.allow_backend:
        sys.exit(':8001 백엔드가 떠 있다. SAM2/LaMa 가 GPU 를 이중으로 점유하므로 먼저 내려야 한다.')
    for name, url in (('uLayout', S.ulayout_url), ('Omni3D', S.omni_url)):
        try:
            log(S, f'{name} health: {requests.get(url + "/health", timeout=5).json()}')
        except Exception as e:
            sys.exit(f'{name} {url} 에 연결할 수 없다: {e}')

    (out / 'env_before.txt').write_text(env_report.report('(측정 전)'), encoding='utf-8')
    log(S, f'측정 전 GPU 사용 {env_report.gpu_used_mib()} MiB')
    S.rec = Recorder(out / 'raw.csv')

    # SAM3D 파이프라인은 라우터가 app.state 에 캐시하는 것과 같이 한 번만 올린다.
    from services import sam3d_runner
    g = Ctx('_global', 0, 'warmup', '_global')
    with timed(S, g, 'load_SAM3D'):
        S.pipeline = sam3d_runner.load_pipeline()
    log(S, f'SAM3D 로드 후 GPU 사용 {env_report.gpu_used_mib()} MiB')

    n_fail = 0
    for sc in scenes:
        log(S, f'===== {sc["id"]} 시작 (GPU {env_report.gpu_used_mib()} MiB) =====')
        for it in range(0, a.warmup + a.repeats):
            phase = 'warmup' if it < a.warmup else 'measure'
            idx = it if phase == 'warmup' else it - a.warmup + 1
            for mode in ('sequential', 'parallel'):
                ctx = Ctx(sc['id'], idx, phase, mode)
                md = (out / 'masks') if (phase == 'warmup' and mode == 'sequential' and it == 0) else None
                t0 = time.perf_counter()
                ok = run_once(S, sc, ctx, md)
                n_fail += (not ok)
                log(S, f'{sc["id"]} it={idx} {phase} {mode}: {"OK" if ok else "FAIL"} '
                       f'({time.perf_counter() - t0:.1f}s, GPU {env_report.gpu_used_mib()} MiB)')

    (out / 'env_after.txt').write_text(env_report.report('(측정 후)'), encoding='utf-8')
    log(S, f'측정 후 GPU 사용 {env_report.gpu_used_mib()} MiB / 실패 실행 {n_fail}건')
    S.rec._f.close()
    try:
        import summarize_timing
        summarize_timing.main([str(out / 'raw.csv'), '--repeats', str(a.repeats)])
    except Exception as e:
        log(S, f'표 생성 실패(raw.csv 는 남아 있음): {e}')


if __name__ == '__main__':
    main()
