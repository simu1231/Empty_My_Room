#!/usr/bin/env python
"""단일 GPU 자원 경합 측정 (논문 표 5용, v2). 기존 파이프라인 코드는 수정하지 않는다.

SAM3D 는 이 프로세스에 직접 올리고(services/sam3d_runner), 사이드카(uLayout :8002, Omni3D :8003)는
별도 프로세스로 떠 있는 것을 쓴다. 이 프로세스는 과거 "백엔드" 프로세스와 같은 역할이다
(SAM3D 파이프라인 + 선택적으로 SD / SAM2 / LaMa 를 함께 들고 있다).

조건 (GPU 는 전부 같은 RTX 4090 한 장)
  a   SAM3D 단독                                       사이드카 없음
  b   + 사이드카 상주(유휴)
  c1  b + SD/ControlNet 상시 로드                       (이전 방식, SD 만)
  c2  b + SD + SAM2 + LaMa 상시 로드                    (과거 백엔드와 같은 상주 구성)
  d   b + SD 필요 시 로드·해제 (현행)                    반복마다 SD 로드→추론→해제 후 SAM3D
  e   b + Omni3D 1회를 SAM3D 와 같은 순간에 호출         (프런트의 실제 호출 패턴)
  f1  c1 + e          f2  c2 + e                        (f2 가 과거 관찰과 가장 가까운 후보)
  g1  c1 + Omni3D 연속 호출   g2  c2 + Omni3D 연속 호출   (SAM3D 전 구간 겹침: 최악의 상한, 비현실적 스트레스)
  b_end  b 재측정 (시간 경과에 따른 변화 점검)

반복: 기본 워밍업 1 + 측정 5. c*, f*, g* 는 워밍업 1 + 측정 10. 워밍업은 CSV 에 warmup 으로 남기고 집계에서 뺀다.

안전장치: 연속 2회 실패(OOM 등 어떤 예외든) 또는 연속 2회 실행시간 300초 초과면 그 조건을 중단하고 기록한다.
실패한 실행은 다시 돌리지 않는다. 실패 뒤에는 서비스와 같이 SAM3D 파이프라인을 버리고 다시 올린 다음 다음 반복으로 간다.
조건 사이에는 GPU 메모리와 프로세스가 정리됐는지 확인하고, 정리되지 않았으면 전체를 멈춘다.
"""
import argparse
import csv
import gc
import io
import json
import os
import re
import signal
import socket
import statistics as st
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / 'sam3d' / 'backend'
os.environ.setdefault('LIDRA_SKIP_INIT', 'true')
os.environ.setdefault('CUDA_HOME', os.environ.get('CONDA_PREFIX', ''))
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

OMNI_URL = 'http://localhost:8003'
SLOW_S = 300.0              # 이 시간을 넘긴 실행은 "느린 실행"
WATCHDOG_S = 600.0          # 실행 하나가 이 시간을 넘기면 프로세스를 강제 종료한다(실행 도중에도 작동)
EXIT_WATCHDOG = 97          # 워치독이 종료시켰을 때의 종료 코드
GUARD_N = 2                 # 연속 이 횟수면 조건 중단
CLEAN_TOL_MIB = 300         # 조건 사이 정리 확인: 파이프라인만 남았을 때 대비 허용 오차

# desc / sidecars(필요 여부) / res(측정 프로세스 상주 모델) / omni(none|once|loop) / sdcycle / reps / purpose
SPEC = {
    'a':  dict(desc='SAM3D 단독', sidecars=False, res=(), omni='none', sdcycle=False, reps=5,
               purpose='기준(SAM3D 자체)'),
    'b':  dict(desc='+ 사이드카 상주(유휴)', sidecars=True, res=(), omni='none', sdcycle=False, reps=5,
               purpose='b−a = 사이드카 상주 효과'),
    'c1': dict(desc='+ SD 상시 로드', sidecars=True, res=('sd',), omni='none', sdcycle=False, reps=10,
               purpose='c1−b = SD 상시 로드 효과'),
    'c2': dict(desc='+ SD·SAM2·LaMa 상시 로드', sidecars=True, res=('sd', 'sam2', 'lama'), omni='none',
               sdcycle=False, reps=10, purpose='과거 백엔드와 같은 상주 구성. c2−c1 = SAM2·LaMa 몫'),
    'd':  dict(desc='+ SD 필요 시 로드·해제(현행)', sidecars=True, res=(), omni='none', sdcycle=True, reps=5,
               purpose='현행. SAM3D 구간은 b 와 같은 구성이고 핵심은 SD 구간 비용'),
    'e':  dict(desc='+ Omni3D 1회 동시 호출', sidecars=True, res=(), omni='once', sdcycle=False, reps=5,
               purpose='프런트의 실제 호출 패턴(겹침은 약 0.3초)'),
    'f1': dict(desc='c1 + Omni3D 1회', sidecars=True, res=('sd',), omni='once', sdcycle=False, reps=10,
               purpose='실제 호출 패턴 + SD 상주'),
    'f2': dict(desc='c2 + Omni3D 1회', sidecars=True, res=('sd', 'sam2', 'lama'), omni='once', sdcycle=False,
               reps=10, purpose='과거 구성 + 실제 호출 패턴(과거 관찰과 가장 가까운 후보)'),
    'g1': dict(desc='c1 + Omni3D 연속', sidecars=True, res=('sd',), omni='loop', sdcycle=False, reps=10,
               purpose='SAM3D 전 구간 겹침(최악의 상한, 스트레스)'),
    'g2': dict(desc='c2 + Omni3D 연속', sidecars=True, res=('sd', 'sam2', 'lama'), omni='loop', sdcycle=False,
               reps=10, purpose='과거 구성 + 전 구간 겹침(최악의 상한, 스트레스)'),
    'b_end': dict(desc='b 재측정', sidecars=True, res=(), omni='none', sdcycle=False, reps=5,
                  purpose='b 와 비교해 시간 경과에 따른 변화 점검'),
}
ORDER = list(SPEC)
HEAVY = ('c1', 'c2', 'f1', 'f2', 'g1', 'g2')


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', port)) == 0


class GpuSampler:
    """nvidia-smi -lms 100 스트리밍: 전체 VRAM(모든 프로세스 합), SM 클럭, 온도, 전력."""

    Q = 'memory.used,memory.total,clocks.sm,temperature.gpu,power.draw'

    def __init__(self):
        self.samples = []       # (t, used, sm_clock, temp, power)
        self.total_mib = None
        self._p = subprocess.Popen(
            ['nvidia-smi', f'--query-gpu={self.Q}', '--format=csv,noheader,nounits', '-lms', '100'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self._p.stdout:
            try:
                used, total, clk, temp, pw = (float(x) for x in line.split(','))
            except ValueError:
                continue
            self.total_mib = int(total)
            self.samples.append((time.perf_counter(), int(used), clk, temp, pw))

    def latest_used(self):
        return self.samples[-1][1] if self.samples else None

    def window(self, t0, t1):
        v = [s for s in self.samples if t0 <= s[0] <= t1]
        if not v:
            return None
        return dict(n=len(v), vram_max=max(s[1] for s in v), clk_min=min(s[2] for s in v),
                    temp_max=max(s[3] for s in v), power_max=max(s[4] for s in v))

    def stop(self):
        self._p.terminate()


class Recorder:
    def __init__(self, path):
        new = not Path(path).exists()
        self._f = open(path, 'a', newline='', encoding='utf-8')
        self._w = csv.writer(self._f)
        if new:
            self._w.writerow(['condition', 'iteration', 'phase', 'metric', 'value', 'status', 'note'])
        self._lock = threading.Lock()

    def add(self, cond, it, phase, metric, value, status='ok', note=''):
        with self._lock:
            self._w.writerow([cond, it, phase, metric, '' if value is None else f'{value:.4f}', status, note])
            self._f.flush()


def log(S, msg):
    line = f'[{datetime.now().strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    S.logf.write(line + '\n')
    S.logf.flush()


def sync(torch):
    try:
        torch.cuda.synchronize()
    except Exception:
        pass


def classify(torch, e):
    s = str(e).lower()
    if isinstance(e, getattr(torch.cuda, 'OutOfMemoryError', ())) or 'out of memory' in s:
        return 'OOM'
    return type(e).__name__


def dxg_counts():
    """WSL2 dmesg 의 호스트(WDDM) 할당 거부 누적 수. 읽을 수 없으면 None.
    make_resident -12 = 메모리를 상주시키지 못함(ENOMEM), create_allocation -75 = 할당 거부."""
    try:
        out = subprocess.run(['dmesg'], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    c = {'make_resident': 0, 'create_allocation': 0}
    for l in out.splitlines():
        if 'dxgk' in l and 'failed' in l.lower():
            for k in c:
                if k in l:
                    c[k] += 1
    return c


def dxg_delta(d0, d1):
    return {k: (None if d0 is None or d1 is None else d1[k] - d0[k]) for k in ('make_resident', 'create_allocation')}


class Guard:
    """연속 실패 / 연속 느린 실행 판정. 순수 로직이라 GPU 없이 시험할 수 있다."""

    def __init__(self):
        self.fail = 0
        self.slow = 0

    def update(self, ok, wall_s):
        self.fail = 0 if ok else self.fail + 1
        self.slow = self.slow + 1 if wall_s > SLOW_S else 0
        if self.fail >= GUARD_N:
            return f'연속 {GUARD_N}회 실패'
        if self.slow >= GUARD_N:
            return f'연속 {GUARD_N}회 {SLOW_S:.0f}초 초과'
        return None


class Watchdog:
    """실행 하나가 limit_s 를 넘기면 실패로 기록하고 프로세스를 강제 종료한다.
    안전장치(Guard)는 실행이 끝난 뒤에만 판정하므로, 끝나지 않는 실행은 이것만이 막을 수 있다.
    CUDA 호출에 갇힌 메인 스레드는 파이썬에서 중단시킬 수 없어서 프로세스째 종료한다(os._exit, 5초 뒤 SIGKILL 보강)."""

    def __init__(self, S, limit_s):
        self.S, self.limit = S, limit_s
        self.t_arm, self.ctx, self.d0 = None, None, None
        threading.Thread(target=self._loop, daemon=True).start()

    def arm(self, cond, idx, phase, d0):
        self.ctx, self.d0, self.t_arm = (cond, idx, phase), d0, time.monotonic()

    def disarm(self):
        self.t_arm = None

    def _loop(self):
        while True:
            time.sleep(1.0)
            t = self.t_arm
            if t is not None and time.monotonic() - t > self.limit:
                self._fire(time.monotonic() - t)

    def _fire(self, elapsed):
        S = self.S
        cond, idx, phase = self.ctx
        try:
            note = f'WATCHDOG: 실행이 {self.limit:.0f}초를 넘겨 프로세스를 강제 종료(경과 {elapsed:.0f}초). 이 실행은 완료되지 않았다'
            S.rec.add(cond, idx, phase, 'run_result', elapsed, 'failed', note)
            S.rec.add(cond, idx, phase, 'condition_abort', None, 'failed', note + '; 조건 중단, 재실행 안 함')
            d = dxg_delta(self.d0, dxg_counts())
            for k, v in d.items():
                S.rec.add(cond, 0, 'setup', f'dxg_{k}_fail_delta', v, note='워치독 종료 시점까지의 조건 중 dmesg 호스트 할당 거부 증가 수')
            S.rec.add(cond, idx, phase, 'watchdog_gpu_used_mib', S.gpu.latest_used(), note='종료 직전 nvidia-smi 사용량')
            log(S, f'!! {note} | dmesg 증가 {d}')
        finally:
            S.logf.flush()
            try:
                S.gpu.stop()          # 고아 nvidia-smi 가 남지 않게
            except Exception:
                pass
            threading.Timer(5.0, lambda: os.kill(os.getpid(), signal.SIGKILL)).start()
            os._exit(EXIT_WATCHDOG)


# ── 입력 준비 ───────────────────────────────────────────────────────────────
def prepare_inputs(S, out):
    """SAM2 로 sceneB 소파(SAM3D 입력)와 액자(Omni3D bbox), SD 용 이미지·마스크를 만들고 SAM2 는 바로 내린다."""
    import cv2
    import numpy as np
    from PIL import Image
    from services.extract_service import ExtractService
    from services.sam2_service import SAM2Service
    sc = next(s for s in json.loads((Path(__file__).with_name('scenes.json')).read_text(encoding='utf-8'))['scenes']
              if s['id'] == 'sceneB')
    furn = {f['name']: f['points'] for f in sc['furniture']}
    src = REPO / sc['path']
    img = np.array(Image.open(src).convert('RGB'))
    h, w = img.shape[:2]
    scale = 1024 / max(h, w) if max(h, w) > 1024 else 1.0
    image_np = cv2.resize(img, (int(w * scale), int(h * scale))) if scale != 1.0 else img
    sam2 = SAM2Service()
    pred = sam2.predictor
    pred.set_image(image_np)

    def best_mask(points, close):
        pts = np.array([[int(p[0] * scale), int(p[1] * scale)] for p in points])
        masks, _, _ = pred.predict(point_coords=pts, point_labels=np.ones(len(pts), int), multimask_output=True)
        m = (masks[int(np.argmax([x.sum() for x in masks]))].astype(np.uint8)) * 255
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((close, close), np.uint8))
        cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(m, cs, -1, 255, thickness=cv2.FILLED)
        return m

    ex = ExtractService()
    sofa = ex.refine_mask(best_mask(furn['소파'], 25), edge_blur=3)
    crop, _ = ex.extract_furniture(image_np, sofa)
    buf = io.BytesIO()
    crop.save(buf, format='PNG')
    frame = best_mask(furn['액자'], 5)
    rows, cols = np.where(frame > 127)
    bbox = [round(cols.min() / scale, 1), round(rows.min() / scale, 1),
            round(cols.max() / scale, 1), round(rows.max() / scale, 1)]
    sd_mask = sam2.predict(image_np, [[int(p[0] * scale), int(p[1] * scale)] for p in furn['소파']])['mask']
    S.sofa_png, S.image_np, S.sd_mask = buf.getvalue(), image_np, sd_mask
    S.omni_img, S.omni_bbox = src.read_bytes(), bbox
    (out / 'input_sofa.png').write_bytes(S.sofa_png)
    np.save(out / 'input_image.npy', image_np)       # 표 6 자식 프로세스가 같은 입력을 쓴다
    np.save(out / 'input_mask.npy', sd_mask)
    del sam2, pred
    gc.collect()
    S.torch.cuda.empty_cache()
    log(S, f'입력 준비 완료: 소파 PNG {len(S.sofa_png)}B, 액자 bbox(원본px) {bbox}, SAM2 해제')


# ── SAM3D 파이프라인 (실패 시 서비스와 같이 버리고 다시 올린다) ──────────────────
def load_sam3d(S):
    from services import sam3d_runner
    S.sam3d = sam3d_runner
    t = time.perf_counter()
    S.pipeline = sam3d_runner.load_pipeline()
    sync(S.torch)
    dt = time.perf_counter() - t
    for name, bucket in (('decode_slat', S.dec), ('sample_sparse_structure', S.ss), ('sample_slat', S.slat)):
        orig = getattr(S.pipeline, name)

        def w(*a, _o=orig, _b=bucket, **k):
            sync(S.torch)
            t0 = time.perf_counter()
            r = _o(*a, **k)
            sync(S.torch)
            _b.append((t0, time.perf_counter()))
            return r
        setattr(S.pipeline, name, w)
    return dt


def drop_sam3d(S):
    S.pipeline = None
    gc.collect()
    S.torch.cuda.empty_cache()


# ── 한 번의 SAM3D 실행 ───────────────────────────────────────────────────────
def overlap(a, b):
    return sum(max(0.0, min(e1, e2) - max(s1, s2)) for s1, e1 in a for s2, e2 in b)


def run_sam3d(S, cond, it, phase, omni):
    import requests
    torch = S.torch
    stop = threading.Event()
    om = {'n': 0, 'fail': 0, 'win': []}

    def omni_call():
        t = time.perf_counter()
        try:
            r = requests.post(f'{OMNI_URL}/estimate',
                              files={'image': ('scene.jpg', S.omni_img, 'image/jpeg')},
                              data={'bbox': ','.join(map(str, S.omni_bbox)), 'category': '액자'}, timeout=300)
            ok = r.ok and r.json().get('success', False)
        except Exception:
            ok = False
        om['win'].append((t, time.perf_counter()))
        om['n'] += 1
        om['fail'] += (not ok)

    def omni_worker():
        if omni == 'once':
            omni_call()
        else:
            while not stop.is_set():
                omni_call()

    S.dec.clear(); S.ss.clear(); S.slat.clear()
    torch.cuda.reset_peak_memory_stats()
    th = threading.Thread(target=omni_worker, daemon=True) if omni != 'none' else None
    sync(torch)
    t0 = time.perf_counter()
    if th:
        th.start()
    ok, reason = True, ''
    try:
        S.sam3d.generate(S.pipeline, S.sofa_png, '소파')
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            raise
        ok, reason = False, f'{classify(torch, e)}: {str(e)[:160]}'
        log(S, f'!! 실패 {cond} it={it} {phase}: {reason}')
        S.logf.write(traceback.format_exc() + '\n')
        del e
    sync(torch)
    t1 = time.perf_counter()
    stop.set()
    if th:
        th.join(timeout=300)
    w = S.gpu.window(t0, t1)
    tot = S.gpu.total_mib
    st_ = 'ok' if ok else 'failed'
    R = S.rec
    R.add(cond, it, phase, 'sam3d_total_s', t1 - t0, st_, reason)
    R.add(cond, it, phase, 'sam3d_decode_s', sum(e - s for s, e in S.dec), st_,
          f'호출 {len(S.dec)}회 {[round(e - s, 3) for s, e in S.dec]}')
    R.add(cond, it, phase, 'sam3d_sample_ss_s', sum(e - s for s, e in S.ss), st_)
    R.add(cond, it, phase, 'sam3d_sample_slat_s', sum(e - s for s, e in S.slat), st_)
    if w:
        R.add(cond, it, phase, 'vram_max_mib', w['vram_max'], st_, f'nvidia-smi 100ms 샘플 {w["n"]}개')
        R.add(cond, it, phase, 'vram_free_min_mib', tot - w['vram_max'], st_)
        R.add(cond, it, phase, 'sm_clock_min_mhz', w['clk_min'], st_)
        R.add(cond, it, phase, 'gpu_temp_max_c', w['temp_max'], st_)
        R.add(cond, it, phase, 'gpu_power_max_w', w['power_max'], st_)
    R.add(cond, it, phase, 'proc_max_allocated_mib', torch.cuda.max_memory_allocated() / 2**20, st_)
    R.add(cond, it, phase, 'proc_max_reserved_mib', torch.cuda.max_memory_reserved() / 2**20, st_)
    if omni != 'none':
        lat = [e - s for s, e in om['win']]
        R.add(cond, it, phase, 'omni3d_calls', om['n'], st_, f'실패 {om["fail"]}')
        R.add(cond, it, phase, 'omni3d_latency_max_s', max(lat) if lat else None, st_)
        R.add(cond, it, phase, 'omni3d_overlap_decode_s', overlap(om['win'], S.dec), st_,
              'Omni3D 호출 구간과 SAM3D decode_slat 구간이 겹친 시간')
    gc.collect()
    torch.cuda.empty_cache()
    return ok, reason, t1 - t0


# ── SD 한 사이클 / 상주 모델 ─────────────────────────────────────────────────
def sd_cycle(S, cond, it, phase, keep=False):
    from services.sd_service import SDService
    torch = S.torch
    torch.cuda.reset_peak_memory_stats()
    sync(torch)
    ts = time.perf_counter()
    t = ts
    sd = SDService()
    sync(torch)
    S.rec.add(cond, it, phase, 'sd_load_s', time.perf_counter() - t)
    t = time.perf_counter()
    sd.inpaint(S.image_np, S.sd_mask)
    sync(torch)
    S.rec.add(cond, it, phase, 'sd_infer_s', time.perf_counter() - t)
    S.rec.add(cond, it, phase, 'sd_max_allocated_mib', torch.cuda.max_memory_allocated() / 2**20)
    if keep:
        return sd
    t = time.perf_counter()
    del sd
    gc.collect()
    torch.cuda.empty_cache()
    sync(torch)
    S.rec.add(cond, it, phase, 'sd_unload_s', time.perf_counter() - t)
    w = S.gpu.window(ts, time.perf_counter())
    if w:
        S.rec.add(cond, it, phase, 'sd_phase_vram_max_mib', w['vram_max'], note='SD 로드~해제 구간 nvidia-smi 최댓값')
    return None


def load_residents(S, cond, res):
    """측정 프로세스에 SD / SAM2 / LaMa 를 올려 둔다. 올리는 도중 실패하면 예외를 그대로 올린다."""
    torch = S.torch
    objs = {}
    a0 = torch.cuda.memory_allocated() / 2**20
    if 'sd' in res:
        objs['sd'] = sd_cycle(S, cond, 0, 'setup', keep=True)       # 한 번 돌려서 커널 초기화까지 끝낸 상주 상태
    if 'sam2' in res:
        from services.sam2_service import SAM2Service
        objs['sam2'] = SAM2Service()
    if 'lama' in res:
        from services.lama_service import LamaService
        objs['lama'] = LamaService()
    gc.collect()
    torch.cuda.empty_cache()
    sync(torch)
    time.sleep(1.0)
    S.rec.add(cond, 0, 'setup', 'resident_alloc_mib', torch.cuda.memory_allocated() / 2**20 - a0,
              note=f'상주 {",".join(res)}: 이 프로세스에서 늘어난 할당량')
    S.rec.add(cond, 0, 'setup', 'vram_used_after_residents_mib', S.gpu.latest_used())
    return objs


def stray_processes():
    try:
        import psutil
    except ImportError:
        return []
    me = os.getpid()
    out = []
    for p in psutil.process_iter(['pid', 'cmdline']):
        cl = p.info['cmdline'] or []
        # 파이썬 인터프리터가 이 스크립트들을 실행 중인 프로세스만 센다(스크립트 이름이 명령줄에 들어간 셸은 제외).
        if p.info['pid'] != me and cl and os.path.basename(cl[0]).startswith('python') \
                and any(os.path.basename(x) in ('measure_gpu_contention.py', 'measure_sd_cost.py') for x in cl[1:]):
            out.append(p.info['pid'])
    return out


def cleanup_check(S, cond, label=''):
    """조건 사이 정리 확인. 파이프라인만 남아 있어야 한다. 정리되지 않았으면 False."""
    torch = S.torch
    for attempt in range(2):
        gc.collect()
        torch.cuda.empty_cache()
        sync(torch)
        time.sleep(3.0)
        alloc = torch.cuda.memory_allocated() / 2**20
        smi = S.gpu.latest_used()
        leak = alloc - S.base_alloc_mib
        stray = stray_processes()
        ok = leak <= CLEAN_TOL_MIB and not stray
        S.rec.add(cond, 0, f'cleanup{label}', 'cleanup_leak_mib', leak, 'ok' if ok else 'failed',
                  f'allocated {alloc:.0f} / 기준 {S.base_alloc_mib:.0f} MiB, nvidia-smi {smi} MiB, 다른 측정 프로세스 {stray}')
        log(S, f'  정리 확인[{cond}{label}] 시도 {attempt + 1}: 할당 {alloc:.0f} MiB(기준 대비 {leak:+.0f}), '
               f'nvidia-smi {smi} MiB, 다른 측정 프로세스 {stray or "없음"} → {"정상" if ok else "비정상"}')
        if ok:
            return True
    return False


# ── 조건 하나 ───────────────────────────────────────────────────────────────
def run_condition(S, cond, reps, warmup, run_fn):
    """run_fn(S, cond, idx, phase) -> (ok, reason, wall_s). 시험에서는 가짜 함수를 넣는다."""
    spec = SPEC[cond]
    d0 = dxg_counts()
    idle = S.gpu.latest_used()
    S.rec.add(cond, 0, 'setup', 'vram_idle_before_mib', idle, note='조건 시작 전 nvidia-smi 사용량')
    log(S, f'===== 조건 {cond}: {spec["desc"]} | 반복 워밍업 {warmup}+측정 {reps} | 시작 전 GPU {idle} MiB =====')
    objs = {}
    guard = Guard()
    attempted = succeeded = 0
    abort = None
    try:
        try:
            if spec['res']:
                objs = load_residents(S, cond, spec['res'])
                log(S, f'  상주 {",".join(spec["res"])} 후 GPU {S.gpu.latest_used()} MiB')
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            reason = f'{classify(S.torch, e)}: {str(e)[:160]}'
            S.rec.add(cond, 0, 'setup', 'run_result', 0, 'failed', f'상주 모델 로드 실패 — {reason}')
            S.rec.add(cond, 0, 'setup', 'condition_abort', None, 'failed', f'상주 모델 로드 실패: {reason}')
            log(S, f'!! 조건 {cond} 상주 모델 로드 실패: {reason}')
            S.logf.write(traceback.format_exc() + '\n')
            return
        for i in range(warmup + reps):
            phase = 'warmup' if i < warmup else 'measure'
            idx = i if phase == 'warmup' else i - warmup + 1
            t0 = time.perf_counter()
            wd = getattr(S, 'wd', None)
            if wd:
                wd.arm(cond, idx, phase, d0)
            ok, reason, wall = run_fn(S, cond, idx, phase)
            if wd:
                wd.disarm()
            attempted += 1
            succeeded += (ok and phase == 'measure')
            S.rec.add(cond, idx, phase, 'run_result', wall, 'ok' if ok else 'failed', reason)
            log(S, f'{cond} it={idx} {phase}: {"ok" if ok else "FAILED"} wall {wall:.1f}s '
                   f'({time.perf_counter() - t0:.1f}s 경과) GPU {S.gpu.latest_used()} MiB {reason}')
            abort = guard.update(ok, wall)
            if not ok and S.reload_after_fail:
                drop_sam3d(S)                       # 서비스와 같은 동작: 실패하면 파이프라인을 버리고 다시 올린다
                dt = load_sam3d(S)
                S.rec.add(cond, idx, phase, 'sam3d_reload_s', dt, note='실패 후 파이프라인 재로드(측정 시간에서 제외)')
                S.base_alloc_mib = S.torch.cuda.memory_allocated() / 2**20
            if abort:
                S.rec.add(cond, idx, phase, 'condition_abort', None, 'failed',
                          f'{abort}: 중단. 시도 {attempted}회(워밍업 포함), 측정 성공 {succeeded}회')
                log(S, f'!! 조건 {cond} 중단 — {abort} (시도 {attempted}, 측정 성공 {succeeded})')
                break
    finally:
        objs.clear()
        for k, v in dxg_delta(d0, dxg_counts()).items():
            S.rec.add(cond, 0, 'setup', f'dxg_{k}_fail_delta', v, note='조건 중 dmesg 호스트 할당 거부 증가 수(조건 종료 시점 집계)')
        if not S.skip_cleanup_gpu:
            clean = cleanup_check(S, cond)
            if not clean:
                S.rec.add(cond, 0, 'cleanup', 'cleanup_abort', None, 'failed', '정리되지 않아 전체 중단')
                raise SystemExit(f'조건 {cond} 뒤 GPU 메모리/프로세스가 정리되지 않았다 — 전체 중단')


def real_run(S, cond, idx, phase):
    spec = SPEC[cond]
    if spec['sdcycle']:
        try:
            sd_cycle(S, cond, idx, phase, keep=False)       # 현행: 쓰고 바로 해제한 뒤 SAM3D
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            reason = f'SD 단계 {classify(S.torch, e)}: {str(e)[:160]}'
            S.logf.write(traceback.format_exc() + '\n')
            gc.collect()
            S.torch.cuda.empty_cache()
            return False, reason, 0.0
    return run_sam3d(S, cond, idx, phase, spec['omni'])


# ── 요약 ────────────────────────────────────────────────────────────────────
def fmt(v):
    if not v:
        return '—'
    return f'{st.mean(v):.2f}±{st.stdev(v):.2f}' if len(v) > 1 else f'{v[0]:.2f}±n/a'


def med(v):
    return f'{st.median(v):.2f} ({min(v):.2f}~{max(v):.2f})' if v else '—'


def summarize(out):
    rows = list(csv.DictReader((out / 'raw.csv').open(encoding='utf-8')))
    conds = [c for c in ORDER if any(r['condition'] == c for r in rows)]

    def vals(c, metric, ok_only=True):
        return [float(r['value']) for r in rows if r['condition'] == c and r['metric'] == metric
                and r['phase'] == 'measure' and (r['status'] == 'ok' or not ok_only) and r['value'] != '']

    def results(c):
        return [r for r in rows if r['condition'] == c and r['metric'] == 'run_result']

    def info(c):
        rr = results(c)
        att = [r for r in rr if r['phase'] in ('warmup', 'measure')]
        m_att = [r for r in att if r['phase'] == 'measure']
        m_ok = [r for r in m_att if r['status'] == 'ok']
        abort = next((r for r in rows if r['condition'] == c and r['metric'] == 'condition_abort'), None)
        return dict(att=len(att), m_att=len(m_att), m_ok=len(m_ok), abort=abort,
                    failed=[r for r in att if r['status'] == 'failed'], planned=SPEC[c]['reps'])

    L = ['# 표 5 단일 GPU 자원 경합 측정', '',
         '**모든 수치는 이번에 측정한 실제 값**이다(기존 기록은 마지막 절에 따로 있다). 워밍업 1회는 집계에서 뺐고, 평균±표본표준편차는 **성공한 측정 실행만**으로 계산했다. '
         '실패·중단된 조건은 표 5-B 에 따로 있다. GPU 는 전 조건 같은 RTX 4090 24GB 한 장이다.', '',
         '## 표 5-A. 조건별 결과 (성공한 측정 실행만)', '',
         '| 조건 | 상주(측정 프로세스) | 동시 GPU 작업 | 성공/계획 | VRAM 최댓값 (MiB) | 최소 여유 (MiB) | 프로세스 max_allocated (MiB) | SAM3D 디코드 (s) | SAM3D 전체 (s) | Omni3D·디코드 겹침 (s) |',
         '|---|---|---|---|---|---|---|---|---|---|']
    omni_txt = {'none': '없음', 'once': 'Omni3D 1회', 'loop': 'Omni3D 연속'}
    for c in conds:
        i, sp = info(c), SPEC[c]
        resid = 'SAM3D' + (' + ' + ' + '.join(x.upper() for x in sp['res']) if sp['res'] else '') + \
                (' (SD 반복마다 로드·해제)' if sp['sdcycle'] else '')
        L.append(f'| **{c}** {sp["desc"]} | {resid} | {omni_txt[sp["omni"]]} | {i["m_ok"]}/{i["planned"]} | '
                 f'{fmt(vals(c, "vram_max_mib"))} | {fmt(vals(c, "vram_free_min_mib"))} | '
                 f'{fmt(vals(c, "proc_max_allocated_mib"))} | {fmt(vals(c, "sam3d_decode_s"))} | '
                 f'{fmt(vals(c, "sam3d_total_s"))} | {fmt(vals(c, "omni3d_overlap_decode_s"))} |')
    L += ['', '- 성공/계획의 n 이 조건마다 다르다(5 또는 10). 중단된 조건은 n 이 계획보다 작다.',
          '- **VRAM 최댓값 / 최소 여유**: SAM3D 실행 구간의 `nvidia-smi -lms 100` 샘플 중 모든 프로세스 합의 최댓값, 24,564 MiB 에서 뺀 값. '
          '캐싱 할당자가 남는 공간을 채우므로 "여유"는 필요량이 아니다. 100 ms 보다 짧은 스파이크는 놓칠 수 있다.',
          '- **프로세스 max_allocated**: 이 측정 프로세스의 `torch.cuda.max_memory_allocated`(실행마다 초기화).',
          '- **사이드카(uLayout, Omni3D)별 max_memory_allocated 는 확인 불가**(사이드카 코드를 고쳐야 하고 WSL2 nvidia-smi 는 프로세스별 값을 N/A 로 준다).',
          '- **SAM3D 디코드**: `pipeline.decode_slat` 호출 시간(동기화 후, 코드는 수정하지 않고 감쌈).',
          '- **Omni3D·디코드 겹침**: Omni3D 요청 구간과 디코드 구간이 실제로 겹친 시간. 조건 e, f 는 요청이 약 0.3초뿐이라 거의 0 이 정상이다.',
          '- 조건 d 의 SAM3D 구간은 SD 를 해제한 뒤라 b 와 같은 구성이다. d 의 SD 구간 비용은 raw.csv 의 `sd_*` 행에 있다.', '',
          '### 조건이 비교하는 것', '', '| 조건 | 비교 목적 |', '|---|---|']
    L += [f'| {c} | {SPEC[c]["purpose"]} |' for c in conds]
    L += ['', '## 표 5-A2. 분포 (중앙값 (최소~최대), 성공한 측정 실행)', '',
          '| 조건 | 디코드 (s) | SAM3D 전체 (s) | SM 클럭 최소 (MHz) | GPU 온도 최대 (℃) |', '|---|---|---|---|---|']
    for c in conds:
        L.append(f'| {c} | {med(vals(c, "sam3d_decode_s"))} | {med(vals(c, "sam3d_total_s"))} | '
                 f'{med(vals(c, "sm_clock_min_mhz"))} | {med(vals(c, "gpu_temp_max_c"))} |')
    L += ['', '## 표 5-B. 실패·중단 (성공 결과와 구분, 재실행하지 않았다)', '']
    bad = [c for c in conds if info(c)['failed'] or info(c)['abort']]
    if not bad:
        L.append('실패·중단된 조건 없음.')
    else:
        L += ['| 조건 | 시도(워밍업 포함) | 측정 시도 | 측정 성공 | 실패 횟수 | 중단 | 오류 원인 | make_resident -12 증가 |',
              '|---|---|---|---|---|---|---|---|']
        for c in bad:
            i = info(c)
            dxg = next((r for r in rows if r['condition'] == c and r['metric'] == 'dxg_make_resident_fail_delta'), None)
            reasons = '; '.join(sorted({r['note'][:90] for r in i['failed']})) or '—'
            L.append(f'| {c} | {i["att"]} | {i["m_att"]} | {i["m_ok"]} | {len(i["failed"])} | '
                     f'{i["abort"]["note"] if i["abort"] else "아니오"} | {reasons} | {dxg["value"] if dxg and dxg["value"] else "확인 불가"} |')
    L += ['', '## 회차별 원본값', '', '| 조건 | 회차 | 결과 | wall(s) | VRAM 최댓값 | 최소 여유 | max_allocated | 디코드(s) | 비고 |',
          '|---|---|---|---|---|---|---|---|---|']
    for c in conds:
        for r in results(c):
            if r['phase'] not in ('warmup', 'measure'):
                continue
            key = (c, r['phase'], r['iteration'])

            def g(m):
                for x in rows:
                    if (x['condition'], x['phase'], x['iteration'], x['metric']) == (key[0], key[1], key[2], m):
                        return x['value']
                return ''
            L.append(f'| {c} | {"W" if r["phase"] == "warmup" else r["iteration"]} | {r["status"]} | {float(r["value"]):.1f} | '
                     f'{g("vram_max_mib")} | {g("vram_free_min_mib")} | {g("proc_max_allocated_mib")} | '
                     f'{g("sam3d_decode_s")} | {r["note"][:70]} |')
    L += ['', '## 정리 확인 (조건 사이)', '', '| 조건 | 기준 대비 잔여 할당(MiB) | 결과 | 내용 |', '|---|---|---|---|']
    for r in rows:
        if r['metric'] == 'cleanup_leak_mib':
            L.append(f'| {r["condition"]} | {float(r["value"]):+.0f} | {r["status"]} | {r["note"]} |')
    ph = [r for r in rows if r['metric'] == 'dxg_make_resident_posthoc']
    if ph:
        L += ['', '## 호스트(WDDM) 할당 거부 — 조건별 (dmesg `dxgkio_make_resident: Ioctl failed: -12`, 사후 대조)', '',
              '| 조건 | 건수 | 발생 시각 |', '|---|---|---|']
        L += [f'| {r["condition"]} | {int(float(r["value"]))} | {r["note"].split(": ", 1)[-1]} |' for r in ph]
        L += ['', '- dmesg 타임스탬프를 `/proc/uptime` 으로 시계 시각에 맞추고 run.log 의 조건 구간과 대조했다(±2초). 이 집계는 증가 수일 뿐 원인 판정이 아니다.']
    L += ['', '## 과거 기록과 이번 측정 (구분해서 읽을 것)', '',
          '| 항목 | 과거 관찰 [기록, 이번 측정 아님] | 이번 측정 [측정] | 비고 |', '|---|---|---|---|']
    c2 = vals('c2', 'vram_free_min_mib')
    L.append(f'| VRAM 여유 | 약 2.4GB (커밋 ec74862, 원 로그 없음) | c2 최소 여유 {fmt(c2)} MiB (n={len(c2)}), '
             f'c1 {fmt(vals("c1", "vram_free_min_mib"))} MiB | 구성이 완전히 같다는 보장 없음 |')
    allv = [x for c in conds for x in vals(c, 'sam3d_decode_s')]
    if allv:
        L.append(f'| 디코드 시간 | 0.2초 → 67.7초 (커밋 ec74862, 원 로그 없음) | a {fmt(vals("a", "sam3d_decode_s"))} s, '
                 f'전 조건 최댓값 {max(allv):.2f} s | 과거와 같은 코드·정의인지 확인 불가 |')
    else:
        L.append('| 디코드 시간 | 0.2초 → 67.7초 (커밋 ec74862, 원 로그 없음) | 성공한 측정 없음 | |')
    (out / 'table5.md').write_text('\n'.join(L) + '\n', encoding='utf-8')
    print('\n'.join(L))


def hang_child(out, limit_s):
    """워치독 시험용 자식 프로세스: 일부러 끝나지 않는 실행을 만든다. 워치독이 종료코드 97 로 죽여야 한다."""
    import torch
    out.mkdir(parents=True, exist_ok=True)
    S = type('S', (), {})()
    S.torch, S.logf = torch, open(out / 'run.log', 'w', encoding='utf-8')
    S.rec = Recorder(out / 'raw.csv')
    S.gpu = GpuSampler()
    time.sleep(1.5)
    S.base_alloc_mib = torch.cuda.memory_allocated() / 2**20
    S.reload_after_fail, S.skip_cleanup_gpu = False, False
    S.wd = Watchdog(S, limit_s)
    SPEC['a'] = dict(SPEC['a'], res=())
    held = []

    def hang(S_, cond, idx, phase):
        held.append(torch.empty(1 << 30, dtype=torch.uint8, device='cuda'))   # GPU 1GiB 를 잡고
        while True:                                                            # 끝나지 않는다
            torch.cuda.synchronize()
            time.sleep(1)
    run_condition(S, 'a', 1, 0, hang)


# ── 안전장치 자체 시험 (실험 전) ────────────────────────────────────────────
def selftest(out):
    import torch
    out.mkdir(parents=True, exist_ok=True)
    S = type('S', (), {})()
    S.torch, S.logf = torch, open(out / 'selftest.log', 'w', encoding='utf-8')
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        log(S, f'[{"PASS" if cond else "FAIL"}] {name}')

    # 1) 판정 로직
    def run_seq(seq):
        g, n, ab = Guard(), 0, None
        for ok, wall in seq:
            n += 1
            ab = g.update(ok, wall)
            if ab:
                break
        return n, ab
    check('연속 2회 실패 → 중단', run_seq([(True, 30), (False, 5), (False, 5), (True, 30)])[0] == 3)
    check('실패-성공-실패는 계속', run_seq([(False, 5), (True, 30), (False, 5), (True, 30)])[1] is None)
    check('연속 2회 300초 초과 → 중단', run_seq([(True, 310), (True, 305), (True, 30)])[0] == 2)
    check('301초 → 30초 → 301초는 계속', run_seq([(True, 301), (True, 30), (True, 301)])[1] is None)
    check('정확히 300초는 느린 실행 아님', run_seq([(True, 300), (True, 300)])[1] is None)
    # 2) 실제 CUDA OOM 분류
    base = torch.cuda.memory_allocated()
    reason = None
    try:
        torch.empty(int(512 * 2**30), dtype=torch.uint8, device='cuda')
    except BaseException as e:
        reason = classify(torch, e)
    check(f'실제 CUDA OOM 이 OOM 으로 분류됨 (분류={reason})', reason == 'OOM')
    gc.collect()
    torch.cuda.empty_cache()
    check('OOM 뒤 할당량이 원래대로', abs(torch.cuda.memory_allocated() - base) < 2**20)
    # 3) 통합: 가짜 실행으로 조건 중단·다음 조건 진행·정리 확인
    S.rec = Recorder(out / 'raw.csv')
    S.gpu = GpuSampler()
    time.sleep(1.5)
    S.base_alloc_mib = torch.cuda.memory_allocated() / 2**20
    S.reload_after_fail, S.skip_cleanup_gpu = False, False
    script = {'a': [(False, 'OOM: 가짜', 4.0), (False, 'OOM: 가짜', 4.0), (True, '', 30.0)],
              'b': [(True, '', 310.0), (True, '', 305.0), (True, '', 30.0)],
              'c1': [(True, '', 30.0)] * 4}
    calls = {k: 0 for k in script}

    def fake(S_, cond, idx, phase):
        i = calls[cond]
        calls[cond] += 1
        ok, reason, wall = script[cond][min(i, len(script[cond]) - 1)]
        return ok, reason, wall
    SPEC.update({k: dict(SPEC[k], res=(), sdcycle=False, omni='none') for k in script})   # 시험에서는 모델을 올리지 않는다
    for c in script:
        run_condition(S, c, 3, 1, fake)
    check('a: 가짜 OOM 2연속 뒤 2회 시도에서 중단', calls['a'] == 2)
    check('b: 가짜 300초 초과 2연속 뒤 2회 시도에서 중단', calls['b'] == 2)
    check('c1: 중단된 조건 뒤에도 정상 진행(4회 전부 실행)', calls['c1'] == 4)
    S.rec._f.close()
    summarize(out)
    rows = list(csv.DictReader((out / 'raw.csv').open(encoding='utf-8')))
    check('중단 기록(condition_abort)이 a, b 에만 남음',
          {r['condition'] for r in rows if r['metric'] == 'condition_abort'} == {'a', 'b'})
    check('조건 사이 정리 확인 3회 모두 정상',
          sum(1 for r in rows if r['metric'] == 'cleanup_leak_mib' and r['status'] == 'ok') == 3)
    # 4) 실제 워치독: 끝나지 않는 실행을 만든 자식 프로세스가 종료코드 97 로, 실제로 죽어야 한다
    import shutil
    import psutil
    hang_dir = out / 'hang'
    shutil.rmtree(hang_dir, ignore_errors=True)
    base = S.gpu.latest_used()
    t0 = time.perf_counter()
    p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--selftest-hang-child', str(hang_dir),
                          '--watchdog-s', '4'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        rc = p.wait(timeout=90)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = None
    dt = time.perf_counter() - t0
    check(f'워치독: 끝나지 않는 실행의 프로세스가 종료코드 {EXIT_WATCHDOG} 로 종료됨 (rc={rc}, {dt:.0f}초)', rc == EXIT_WATCHDOG)
    check('워치독: 프로세스가 실제로 사라짐', not psutil.pid_exists(p.pid))
    hrows = list(csv.DictReader((hang_dir / 'raw.csv').open(encoding='utf-8'))) if (hang_dir / 'raw.csv').exists() else []
    check('워치독: 실패 run_result 에 WATCHDOG 가 기록됨',
          any(r['metric'] == 'run_result' and r['status'] == 'failed' and 'WATCHDOG' in r['note'] for r in hrows))
    check('워치독: condition_abort 가 기록됨', any(r['metric'] == 'condition_abort' for r in hrows))
    now = None
    for _ in range(15):
        time.sleep(2)
        now = S.gpu.latest_used()
        if now is not None and base is not None and now <= base + 300:
            break
    check(f'워치독: 종료 뒤 GPU 가 원래대로 (기준 {base} → {now} MiB)', now is not None and now <= base + 300)
    orphans = [q.pid for q in psutil.process_iter(['pid', 'ppid', 'cmdline']) if q.info['ppid'] == 1
               and (q.info['cmdline'] or [''])[0].endswith('nvidia-smi') and '-lms' in ' '.join(q.info['cmdline'] or [])]
    check(f'워치독: 고아 nvidia-smi 샘플러가 남지 않음 (고아 {orphans})', not orphans)
    S.gpu.stop()
    bad = [n for n, ok in results if not ok]
    log(S, f'자체 시험 {len(results) - len(bad)}/{len(results)} 통과')
    return not bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--conditions', default='a')
    ap.add_argument('--repeats', type=int, default=None, help='모든 조건의 측정 횟수를 이 값으로 고정(기본: 조건별 5/10)')
    ap.add_argument('--warmup', type=int, default=1)
    ap.add_argument('--out', default=None)
    ap.add_argument('--summarize-only', default=None, metavar='DIR')
    ap.add_argument('--selftest', action='store_true', help='안전장치 자체 시험(SAM3D 로드 없음)')
    ap.add_argument('--watchdog-s', type=float, default=WATCHDOG_S, help='실행 하나의 강제 종료 한도(초)')
    ap.add_argument('--selftest-hang-child', default=None, metavar='DIR', help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.summarize_only:
        summarize(Path(a.summarize_only))
        return
    if a.selftest_hang_child:
        hang_child(Path(a.selftest_hang_child), a.watchdog_s)
        return
    if a.selftest:
        out = Path(a.out) if a.out else REPO / 'eval' / 'timing_results' / 'contention_selftest'
        sys.exit(0 if selftest(out) else 1)
    conds = [c.strip() for c in a.conditions.split(',') if c.strip()]
    bad = [c for c in conds if c not in SPEC]
    if bad:
        sys.exit(f'알 수 없는 조건: {bad} (가능: {ORDER})')
    for c in conds:
        up = port_open(8002) and port_open(8003)
        down = not port_open(8002) and not port_open(8003)
        if SPEC[c]['sidecars'] and not up:
            sys.exit(f'조건 {c} 는 사이드카(:8002, :8003)가 켜져 있어야 한다.')
        if not SPEC[c]['sidecars'] and not down:
            sys.exit(f'조건 {c} 는 사이드카가 꺼져 있어야 한다.')
    if port_open(8001):
        sys.exit(':8001 백엔드가 떠 있다. SAM2/LaMa 상주분이 섞이므로 먼저 내려야 한다.')
    out = Path(a.out) if a.out else REPO / 'eval' / 'timing_results' / ('contention_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    out.mkdir(parents=True, exist_ok=True)

    import torch
    import env_report
    S = type('S', (), {})()
    S.torch = torch
    S.logf = open(out / 'run.log', 'a', encoding='utf-8')
    S.rec = Recorder(out / 'raw.csv')
    S.gpu = GpuSampler()
    S.reload_after_fail, S.skip_cleanup_gpu = True, False
    S.wd = Watchdog(S, a.watchdog_s)
    time.sleep(1.5)
    log(S, f'워치독 {a.watchdog_s:.0f}초 / 출력 {out} / 조건 {conds} / 사이드카 {"켜짐" if port_open(8002) else "꺼짐"} / dxg 누적 {dxg_counts()}')
    tag = '_'.join(conds)
    (out / f'env_before_{tag}.txt').write_text(env_report.report(f'(조건 {",".join(conds)} 측정 전)'), encoding='utf-8')
    (out / 'precision.txt').write_text(
        'SAM3D: bfloat16 autocast (InferencePipeline 기본 dtype="bfloat16", pipeline.yaml 에서 덮어쓰지 않음)\n'
        'SD inpainting / ControlNet: float16 (services/sd_service.py torch_dtype=torch.float16)\n', encoding='utf-8')

    try:
        prepare_inputs(S, out)
        S.dec, S.ss, S.slat = [], [], []
        dt = load_sam3d(S)
        S.rec.add('_global', 0, 'setup', 'sam3d_load_s', dt)
        S.base_alloc_mib = torch.cuda.memory_allocated() / 2**20
        log(S, f'SAM3D 로드 {dt:.1f}s, 이 프로세스 할당 {S.base_alloc_mib:.0f} MiB, GPU {S.gpu.latest_used()} MiB')
        for c in conds:
            reps = a.repeats or SPEC[c]['reps']
            SPEC[c] = dict(SPEC[c], reps=reps)
            run_condition(S, c, reps, a.warmup, real_run)
    except SystemExit as e:
        log(S, f'전체 중단: {e}')
        S.exit_msg = str(e)
    finally:
        (out / f'env_after_{tag}.txt').write_text(env_report.report(f'(조건 {",".join(conds)} 측정 후)'), encoding='utf-8')
        S.gpu.stop()
        S.rec._f.close()
        summarize(out)


if __name__ == '__main__':
    main()
