"""컨테이너 안의 실제 모델로 세 job_type 전부 큐를 통과시키는 종단 검증.

    docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml up -d
    ~/miniconda3/envs/sam3d/bin/python deploy/test_e2e.py

sam3d를 3번 돌리는 이유는 파이프라인 캐시(콜드/웜)를 보기 위해서이기도 하지만,
워커의 fd 수가 작업마다 **누적되는지**를 확인하기 위해서다. WSL2에서는 공유 GPU
리소스마다 fd가 하나씩 잡혀서, 여기가 늘어나기만 하면 오래 사는 워커가 언젠가
`CUDA driver error`로 죽는다. 자세한 내용은 README의 "3단계에서 실제로 걸린 것".
"""
import os
import sys
import pathlib
import json
import time
import tempfile

import requests

API = "http://localhost:8000"
IMG = os.path.join(tempfile.gettempdir(), "emr_test_input.png")

# 이 파일 기준으로 저장소 루트를 찾는다(개인 홈 경로를 박지 않는다).
REPO_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def ensure_image():
    """테스트 입력 이미지를 직접 만든다.

    예전에는 /tmp에 손으로 만들어 둔 파일을 가리켰는데, /tmp는 주기적으로
    비워지고 리포지터리에도 없어서 남의 머신에서는 그냥 깨졌다. 결정적으로
    생성해두면 아무 준비 없이 실행된다.
    """
    if os.path.exists(IMG):
        return
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (64, 64), (200, 200, 205))      # 밝은 배경
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([8, 26, 56, 52], radius=6, fill=(120, 85, 60))   # 몸통
    d.rounded_rectangle([8, 16, 56, 34], radius=6, fill=(145, 105, 75))  # 등받이
    im.save(IMG)
    print(f"[준비] 테스트 이미지 생성: {IMG}")

JOBS = [
    ("sam3d_mesh",  {"category": "소파"}),
    ("sam3d_mesh",  {"category": "소파"}),          # 2회차 = 파이프라인 캐시(웜)
    ("sam3d_mesh",  {"category": "소파"}),          # 3회차 = fd 누적 확인용
    ("room_layout", {"camera_height_m": "1.6"}),
    ("omni3d",      {"bbox": "10,10,54,54", "category": "액자"}),
]

import subprocess
def worker_fds():
    """워커 프로세스의 열린 fd 수. WSL2에서는 GPU 리소스마다 fd가 하나씩 잡힌다."""
    try:
        out = subprocess.run(
            ["sg","docker","-c",
             "docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml "
             "exec -T worker-sam3d sh -c 'ls /proc/1/fd | wc -l'"],
            capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)
        return int(out.stdout.strip().splitlines()[-1])
    except Exception:
        return -1

def submit(job_type, params):
    with open(IMG, "rb") as f:
        data = {"job_type": job_type, **{k: str(v) for k, v in params.items()}}
        r = requests.post(f"{API}/api/jobs", files={"image": ("in.png", f.read(), "image/png")},
                          data=data, timeout=30)
    r.raise_for_status()
    return r.json()["job_id"]

def poll(job_id, timeout=300):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        r = requests.get(f"{API}/api/jobs/{job_id}", timeout=15).json()
        st = r.get("status")
        if st != last:
            print(f"    [{time.time()-t0:5.1f}s] {st}")
            last = st
        if st in ("done", "failed"):
            return r, time.time() - t0
        time.sleep(1)
    return {"status": "TIMEOUT"}, time.time() - t0

ensure_image()

results = []
for i, (jt, params) in enumerate(JOBS, 1):
    label = f"{jt}{' (웜)' if jt=='sam3d_mesh' and i==2 else ' (콜드)' if jt=='sam3d_mesh' else ''}"
    print(f"\n=== {i}. {label} ===")
    jid = submit(jt, params)
    print(f"    job_id={jid}")
    res, el = poll(jid)
    ok = res.get("status") == "done"
    detail = ""
    if ok and res.get("result_url"):
        rr = requests.get(res["result_url"], timeout=60)
        try:
            payload = rr.json()
            keys = sorted(payload.keys())
            detail = f"keys={keys}"
            if "mesh" in payload:
                m = payload["mesh"]
                detail += f" verts={len(m.get('vertices',[]))} faces={len(m.get('faces',[]))}"
        except Exception as e:
            detail = f"결과 파싱 실패: {e} (본문 {len(rr.content)}B)"
    elif not ok:
        detail = json.dumps(res, ensure_ascii=False)[:300]
    fds = worker_fds() if jt == "sam3d_mesh" else None
    results.append((label, ok, el, detail, fds))
    print(f"    -> {'OK' if ok else 'FAIL'} {el:.1f}s  {detail}"
          + (f"\n       워커 fd 수: {fds}" if fds is not None else ""))

print("\n" + "="*70)
print(f"{'작업':<22}{'결과':<8}{'소요':>8}")
print("-"*70)
for label, ok, el, _, fds in results:
    print(f"{label:<22}{'OK' if ok else 'FAIL':<8}{el:>7.1f}s"
          + (f"   fd={fds}" if fds not in (None, -1) else ""))
print("="*70)
sys.exit(0 if all(r[1] for r in results) else 1)
