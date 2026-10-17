#!/usr/bin/env python
"""SD/ControlNet 로딩·해제 비용 측정 (논문 표 6용). 기존 파이프라인 코드는 수정하지 않는다.

변형 (각각 워밍업 1 + 측정 5, 워밍업은 CSV 에 남기고 집계에서 뺀다)
  cache  새 프로세스, 가중치 파일의 페이지 캐시는 그대로 둔다
  evict  새 프로세스, 시작 직전에 SD/ControlNet 가중치 파일 **각각**에 posix_fadvise(DONTNEED)
         (시스템 전체 drop_caches 는 하지 않는다. root 도 필요 없다)
  warm   한 프로세스 안에서 로드→추론→해제를 반복(첫 반복=워밍업이 import 비용을 치른다)

측정 항목: diffusers import 시간, SDService() 생성 시간, 로드 후 점유(allocated/reserved/nvidia-smi 증가분),
추론 1회 시간과 추론 후 피크, 해제 시간과 해제 후 반환량.

한계(표에 적는다): evict 은 게스트(WSL) 페이지 캐시만 비운다. Windows 호스트의 파일 캐시와 SSD 속도는
통제할 수 없고, 클라우드 EBS 의 콜드 상태를 대표하지 않는다.

사용법: python measure_sd_cost.py --out DIR      (사이드카가 꺼진 상태에서)
"""
import argparse
import csv
import gc
import glob
import json
import os
import statistics as st
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / 'sam3d' / 'backend'
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))
HF = Path(os.path.expanduser('~/.cache/huggingface/hub'))
REPOS = ('models--runwayml--stable-diffusion-inpainting', 'models--lllyasviel--control_v11p_sd15_canny')
WEIGHT_EXT = ('.bin', '.safetensors', '.pth', '.ckpt')


def smi_used():
    out = subprocess.run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                         capture_output=True, text=True).stdout.split()[0]
    return int(out)


# ── 자식 프로세스: 한 번 또는 여러 번의 로드 사이클 ─────────────────────────────
def child(inputs, cycles):
    t_p = time.perf_counter()
    import numpy as np
    import torch
    t_torch = time.perf_counter() - t_p
    t = time.perf_counter()
    torch.zeros(1).cuda()
    torch.cuda.synchronize()
    t_cuda = time.perf_counter() - t
    img, mask = np.load(Path(inputs) / 'input_image.npy'), np.load(Path(inputs) / 'input_mask.npy')
    smi0 = smi_used()
    t = time.perf_counter()
    from diffusers import StableDiffusionControlNetInpaintPipeline, ControlNetModel, DDIMScheduler  # noqa: F401
    t_import = time.perf_counter() - t       # SDService._load 이 첫 호출에서 치르는 import 비용과 같은 import
    from services.sd_service import SDService
    out = []
    for k in range(cycles):
        smi_b = smi_used()
        torch.cuda.reset_peak_memory_stats()
        t = time.perf_counter()
        sd = SDService()
        torch.cuda.synchronize()
        t_load = time.perf_counter() - t
        a1, r1, s1 = torch.cuda.memory_allocated() / 2**20, torch.cuda.memory_reserved() / 2**20, smi_used()
        t = time.perf_counter()
        sd.inpaint(img, mask)
        torch.cuda.synchronize()
        t_infer = time.perf_counter() - t
        pa, pr, s2 = torch.cuda.max_memory_allocated() / 2**20, torch.cuda.max_memory_reserved() / 2**20, smi_used()
        t = time.perf_counter()
        del sd
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
        t_unload = time.perf_counter() - t
        a3, s3 = torch.cuda.memory_allocated() / 2**20, smi_used()
        out.append(dict(cycle=k, load_s=t_load, infer_s=t_infer, unload_s=t_unload,
                        alloc_after_load_mib=a1, reserved_after_load_mib=r1, smi_delta_load_mib=s1 - smi_b,
                        peak_alloc_after_infer_mib=pa, peak_reserved_after_infer_mib=pr, smi_delta_infer_mib=s2 - smi_b,
                        alloc_after_unload_mib=a3, smi_after_unload_minus_before_mib=s3 - smi_b))
    print('RESULT ' + json.dumps(dict(import_torch_s=t_torch, cuda_init_s=t_cuda, import_diffusers_s=t_import,
                                      smi_base_mib=smi0, cycles=out)), flush=True)


# ── 파일별 페이지 캐시 비우기 ─────────────────────────────────────────────────
def weight_files():
    files = []
    for r in REPOS:
        for p in glob.glob(str(HF / r / 'snapshots' / '*' / '**' / '*'), recursive=True):
            rp = os.path.realpath(p)        # HF 캐시는 해시 이름 blob 으로 가는 심볼릭 링크 — 확장자는 링크 이름으로 본다
            if os.path.isfile(rp) and p.endswith(WEIGHT_EXT):
                files.append(rp)
    return sorted(set(files))


def evict(files):
    total = 0
    for f in files:
        fd = os.open(f, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            total += os.fstat(fd).st_size
        finally:
            os.close(fd)
    # 효과 확인: 가장 큰 파일의 64MiB 를 읽어 속도를 본다(그 구간만 다시 캐시에 올라가는 작은 부작용)
    big = max(files, key=os.path.getsize)
    fd = os.open(big, os.O_RDONLY)
    try:
        t = time.perf_counter()
        n = len(os.pread(fd, 64 << 20, 1 << 30))
        dt = time.perf_counter() - t
    finally:
        os.close(fd)
    return total / 2**30, n / 2**30 / dt


class Rec:
    def __init__(self, path):
        self._f = open(path, 'a', newline='', encoding='utf-8')
        self._w = csv.writer(self._f)
        if self._f.tell() == 0:
            self._w.writerow(['variant', 'iteration', 'phase', 'metric', 'value', 'status', 'note'])

    def add(self, variant, it, phase, metric, value, status='ok', note=''):
        self._w.writerow([variant, it, phase, metric, '' if value is None else f'{value:.4f}', status, note])
        self._f.flush()


def stray():
    try:
        import psutil
    except ImportError:
        return []
    out = []
    for p in psutil.process_iter(['pid', 'cmdline']):
        cl = p.info['cmdline'] or []
        if p.info['pid'] != os.getpid() and cl and os.path.basename(cl[0]).startswith('python') \
                and any(os.path.basename(x) in ('measure_sd_cost.py', 'measure_gpu_contention.py') for x in cl[1:]):
            out.append(p.info['pid'])
    return out


def run_child(inputs, cycles):
    p = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--child', '--inputs', str(inputs),
                        '--cycles', str(cycles)], capture_output=True, text=True, timeout=900)
    line = next((l for l in p.stdout.splitlines() if l.startswith('RESULT ')), None)
    if p.returncode != 0 or line is None:
        return None, (p.stderr or p.stdout)[-400:]
    return json.loads(line[7:]), ''


def summarize(out):
    rows = list(csv.DictReader((out / 'raw6.csv').open(encoding='utf-8')))

    def v(variant, metric):
        return [float(r['value']) for r in rows if r['variant'] == variant and r['metric'] == metric
                and r['phase'] == 'measure' and r['status'] == 'ok' and r['value'] != '']

    def f(x):
        return '—' if not x else (f'{st.mean(x):.2f}±{st.stdev(x):.2f}' if len(x) > 1 else f'{x[0]:.2f}±n/a') + f' (n={len(x)})'

    names = {'cache': '새 프로세스·캐시 유지', 'evict': '새 프로세스·파일별 캐시 비움', 'warm': '같은 프로세스 반복(웜)'}
    L = ['# 표 6 SD·ControlNet 로딩 및 해제 비용', '',
         '**이번에 측정한 실제 값**이다(과거 기록은 마지막 절). 워밍업 1회 제외, 평균±표본표준편차(n). GPU 는 RTX 4090 한 장, 사이드카·백엔드는 꺼진 상태, SD/ControlNet 은 fp16.', '',
         '## 표 6-A. 로딩 시간 (초)', '',
         '| 변형 | diffusers import | SDService() 생성(로드) | 첫 호출 합계(import+로드) | 추론 1회 | 해제 |', '|---|---|---|---|---|---|']
    for k in ('cache', 'evict', 'warm'):
        imp = v(k, 'import_diffusers_s')
        tot = [a + b for a, b in zip(imp, v(k, 'load_s'))] if k != 'warm' else []
        L.append(f'| {names[k]} | {f(imp) if k != "warm" else "첫 반복에서만(워밍업)"} | {f(v(k, "load_s"))} | '
                 f'{f(tot) if k != "warm" else "—"} | {f(v(k, "infer_s"))} | {f(v(k, "unload_s"))} |')
    L += ['', '## 표 6-B. GPU 점유 (MiB)', '',
          '| 변형 | 로드 후 allocated | 로드 후 reserved | 로드 후 nvidia-smi 증가분 | 추론 후 피크 allocated | 추론 후 피크 reserved | 추론 후 nvidia-smi 증가분 | 해제 후 nvidia-smi 잔여(기준 대비) |',
          '|---|---|---|---|---|---|---|---|']
    for k in ('cache', 'evict', 'warm'):
        L.append(f'| {names[k]} | {f(v(k, "alloc_after_load_mib"))} | {f(v(k, "reserved_after_load_mib"))} | '
                 f'{f(v(k, "smi_delta_load_mib"))} | {f(v(k, "peak_alloc_after_infer_mib"))} | '
                 f'{f(v(k, "peak_reserved_after_infer_mib"))} | {f(v(k, "smi_delta_infer_mib"))} | '
                 f'{f(v(k, "smi_after_unload_minus_before_mib"))} |')
    ev = v('evict', 'evict_probe_gibs')
    eg = v('evict', 'evict_gib')
    L += ['', '### 측정 방법과 한계', '',
          f'- **evict**: 시작 직전에 가중치 파일 {len(weight_files())}개(약 {eg[0]:.1f} GiB)에 각각 `posix_fadvise(DONTNEED)` 적용 → 가장 큰 파일 64MiB 읽기 속도 {f(ev)} GiB/s '
          '(비우기 전 같은 파일은 약 12 GiB/s, 지난 사전 점검 기준). 시스템 전체 `drop_caches` 는 하지 않았다.' if ev and eg else
          '- **evict**: 효과 확인 값 없음.',
          '- **한계**: 게스트(WSL) 페이지 캐시만 비운다. Windows 호스트 캐시·SSD 속도는 통제하지 못했고, 클라우드 EBS 의 콜드 상태(과거 SD 로드 553초 보고)를 대표하지 않는다.',
          '- **"첫 호출 합계"**: `SDService._load` 가 첫 호출에서 `from diffusers import …` 를 실행하므로 import 와 로드를 더한 값이 새 프로세스에서 사용자가 겪는 첫 로드와 같다.',
          '- **nvidia-smi 증가분**은 로드 직전 대비 증가량(CUDA 컨텍스트 포함 여부는 변형 간 같다). allocated 는 텐서가 차지한 양, reserved 는 할당자가 잡아 둔 양.', '']
    fails = [r for r in rows if r['status'] == 'failed']
    L += [f'## 실패 ({len(fails)}건) — 재실행하지 않았다', ''] + ([f'- {r["variant"]} it={r["iteration"]} {r["phase"]}: {r["note"]}' for r in fails] or ['없음'])
    L += ['', '## 과거 기록과 이번 측정 (구분해서 읽을 것)', '',
          '| 항목 | 과거 관찰 [기록, 이번 측정 아님] | 이번 측정 [측정] |', '|---|---|---|',
          f'| SD 점유 | "3~4GB"(코드 주석·논문 문장), "실측 5.6GB"(커밋 ec74862) — 측정 정의 불명 | 로드 후 allocated {f(v("cache", "alloc_after_load_mib"))} MiB, 추론 후 피크 allocated {f(v("cache", "peak_alloc_after_infer_mib"))} MiB, 추론 후 피크 reserved {f(v("cache", "peak_reserved_after_infer_mib"))} MiB |',
          f'| SD 로드 시간 | 7.6초(주석·면접 노트, 로드/사용 불명), 6.3초(커밋 ec74862), 5.30초(sd_warm.py, 개발 PC) — 원 로그 없음 | 새 프로세스 첫 호출 합계: 캐시 유지 {f([a + b for a, b in zip(v("cache", "import_diffusers_s"), v("cache", "load_s"))])} s, 캐시 비움 {f([a + b for a, b in zip(v("evict", "import_diffusers_s"), v("evict", "load_s"))])} s; 웜 로드 {f(v("warm", "load_s"))} s |',
          '| 과거 값의 재현 여부 | — | 정의(무엇을 어떤 상태에서 쟀는지)가 기록에 없어 일치 여부는 **확인 불가** |']
    (out / 'table6.md').write_text('\n'.join(L) + '\n', encoding='utf-8')
    print('\n'.join(L))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=None)
    ap.add_argument('--child', action='store_true')
    ap.add_argument('--inputs', default=None)
    ap.add_argument('--cycles', type=int, default=1)
    ap.add_argument('--repeats', type=int, default=5)
    ap.add_argument('--warmup', type=int, default=1)
    ap.add_argument('--summarize-only', default=None, metavar='DIR')
    a = ap.parse_args()
    if a.child:
        child(a.inputs, a.cycles)
        return
    if a.summarize_only:
        summarize(Path(a.summarize_only))
        return
    for port in (8001, 8002, 8003):
        import socket
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(('127.0.0.1', port)) == 0:
                sys.exit(f':{port} 이 열려 있다. 표 6 은 서비스가 모두 꺼진 상태에서 잰다.')
    out = Path(a.out) if a.out else REPO / 'eval' / 'timing_results' / ('sdcost_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    out.mkdir(parents=True, exist_ok=True)
    import torch
    import env_report
    import measure_gpu_contention as mgc
    S = type('S', (), {})()
    S.torch, S.logf = torch, open(out / 'run.log', 'a', encoding='utf-8')
    (out / 'env_before.txt').write_text(env_report.report('(표 6 측정 전)'), encoding='utf-8')
    if not (out / 'input_image.npy').exists():
        mgc.prepare_inputs(S, out)
    del S
    gc.collect()
    torch.cuda.empty_cache()
    rec = Rec(out / 'raw6.csv')
    files = weight_files()
    log = S2 = open(out / 'run.log', 'a', encoding='utf-8')

    def say(msg):
        line = f'[{datetime.now().strftime("%H:%M:%S")}] {msg}'
        print(line, flush=True)
        log.write(line + '\n')
        log.flush()
    say(f'가중치 파일 {len(files)}개, {sum(os.path.getsize(f) for f in files) / 2**30:.2f} GiB')
    base_smi = smi_used()
    for variant in ('cache', 'evict'):
        for i in range(a.warmup + a.repeats):
            phase = 'warmup' if i < a.warmup else 'measure'
            idx = i if phase == 'warmup' else i - a.warmup + 1
            if variant == 'evict':
                gib, gibs = evict(files)
                rec.add(variant, idx, phase, 'evict_gib', gib, note=f'{len(files)}개 파일에 fadvise DONTNEED')
                rec.add(variant, idx, phase, 'evict_probe_gibs', gibs, note='가장 큰 파일 64MiB 읽기 속도(GiB/s)')
            res, err = run_child(out, 1)
            if res is None:
                rec.add(variant, idx, phase, 'run_result', None, 'failed', err.replace('\n', ' ')[:300])
                say(f'!! {variant} it={idx} {phase} 실패: {err[-160:]}')
            else:
                for k in ('import_torch_s', 'cuda_init_s', 'import_diffusers_s'):
                    rec.add(variant, idx, phase, k, res[k])
                for k, val in res['cycles'][0].items():
                    if k != 'cycle':
                        rec.add(variant, idx, phase, k, val)
                c = res['cycles'][0]
                say(f'{variant} it={idx} {phase}: import {res["import_diffusers_s"]:.2f}s load {c["load_s"]:.2f}s '
                    f'infer {c["infer_s"]:.2f}s unload {c["unload_s"]:.2f}s | 로드 후 allocated {c["alloc_after_load_mib"]:.0f} MiB')
            time.sleep(2)
            leftover = smi_used() - base_smi
            if abs(leftover) > 300 or stray():
                say(f'!! 정리 비정상: nvidia-smi 기준 대비 {leftover:+d} MiB, 다른 측정 프로세스 {stray()} — 중단')
                rec.add(variant, idx, phase, 'cleanup_abort', None, 'failed', f'{leftover:+d} MiB')
                summarize(out)
                sys.exit(1)
    # 웜: 한 프로세스에서 반복
    res, err = run_child(out, a.warmup + a.repeats)
    if res is None:
        rec.add('warm', 0, 'warmup', 'run_result', None, 'failed', err.replace('\n', ' ')[:300])
        say(f'!! warm 실패: {err[-160:]}')
    else:
        rec.add('warm', 0, 'warmup', 'import_diffusers_s', res['import_diffusers_s'])
        for c in res['cycles']:
            phase = 'warmup' if c['cycle'] < a.warmup else 'measure'
            idx = c['cycle'] if phase == 'warmup' else c['cycle'] - a.warmup + 1
            for k, val in c.items():
                if k != 'cycle':
                    rec.add('warm', idx, phase, k, val)
            say(f'warm it={idx} {phase}: load {c["load_s"]:.2f}s infer {c["infer_s"]:.2f}s unload {c["unload_s"]:.2f}s')
    (out / 'env_after.txt').write_text(env_report.report('(표 6 측정 후)'), encoding='utf-8')
    summarize(out)


if __name__ == '__main__':
    main()
