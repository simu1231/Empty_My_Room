"""측정 환경 기록: GPU/드라이버/CUDA/PyTorch/Python 버전과 측정 시점의 GPU 점유 상태.

단독 실행:  python tools/timing/env_report.py [--out env.txt]
measure_timing.py 가 측정 전후로 report() 를 불러 env_before.txt / env_after.txt 를 만든다.
"""
import argparse
import datetime
import platform
import subprocess
import sys


def _run(cmd):
    try:
        return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                              text=True, timeout=20).stdout.strip()
    except Exception as e:  # nvidia-smi 가 없는 환경에서도 보고서는 만든다
        return f'(실행 실패: {e})'


def gpu_used_mib():
    out = _run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'])
    try:
        return int(out.splitlines()[0])
    except Exception:
        return None


def report(label=''):
    lines = [f'# 측정 환경 {label}', f'시각: {datetime.datetime.now().isoformat(timespec="seconds")}', '']
    lines += ['## GPU (nvidia-smi --query-gpu)',
              _run(['nvidia-smi', '--query-gpu=name,memory.total,memory.used,memory.free,driver_version',
                    '--format=csv']), '']
    lines += ['## GPU 점유 프로세스 (WSL2 는 프로세스별 메모리를 N/A 로 준다)',
              _run(['nvidia-smi', '--query-compute-apps=pid,process_name,used_memory', '--format=csv']), '']
    lines += ['## nvidia-smi 전체', _run(['nvidia-smi']), '']
    lines += ['## 소프트웨어', f'Python: {sys.version}', f'플랫폼: {platform.platform()}']
    try:
        import torch
        lines += [f'PyTorch: {torch.__version__}', f'CUDA(torch 빌드): {torch.version.cuda}',
                  f'cuDNN: {torch.backends.cudnn.version()}',
                  f'torch.cuda.is_available: {torch.cuda.is_available()}',
                  f'장치: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else "-"}']
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            lines.append(f'torch mem_get_info: free={free/2**20:.0f}MiB total={total/2**20:.0f}MiB')
    except Exception as e:
        lines.append(f'torch 정보 실패: {e}')
    lines += ['', '## 리스닝 포트 (8001 backend / 8002 uLayout / 8003 Omni3D)',
              _run("ss -ltnp 2>/dev/null | grep -E ':(8001|8002|8003) '") or '(없음)',
              '', f'git: {_run(["git", "rev-parse", "--short", "HEAD"])} '
                  f'(dirty: {bool(_run(["git", "status", "--porcelain", "--untracked-files=no"]))})']
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--out')
    a = ap.parse_args()
    text = report()
    if a.out:
        open(a.out, 'w', encoding='utf-8').write(text)
    print(text)
