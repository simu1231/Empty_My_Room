"""컨테이너 안의 실제 모델로 세 job_type 전부 큐를 통과시키는 종단 검증.

    docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml up -d
    "${EMR_ROOT:-$HOME}/miniconda3/envs/sam3d/bin/python" deploy/test_e2e.py

EC2 빌더에서는 HOME이 /home/ubuntu 라 ~ 로 적으면 틀린다. 환경은 EMR_ROOT
(/opt/emr) 아래에 풀려 있다 — deploy/aws/config.sh 와 같은 규칙이다.

SAM3D 백엔드(환경변수 E2E_SAM3D, 기본 auto = SAM3D_BACKEND 를 따르고 없으면 local):
  local : 로컬 GPU 추론. 3번 돌려 파이프라인 캐시(콜드/웜)와 fd 누적을 본다(아래).
  fal   : fal.ai 호출을 검증한다. **호출마다 fal 요금이 든다** — 기본 1회(E2E_SAM3D_RUNS 로 조절).
          결과 JSON 의 backend=="fal" 과 메쉬가 비어 있지 않은지를 확인하고, 로컬 파이프라인은
          요구하지 않는다(워커가 fal 모드로 떠 있어야 한다).
  skip  : SAM3D 작업을 건너뛰고 room_layout(uLayout)과 omni3d 만 돌린다.

local 에서 sam3d를 3번 돌리는 이유는 파이프라인 캐시(콜드/웜)를 보기 위해서이기도 하지만,
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

IMG_RGBA = os.path.join(tempfile.gettempdir(), "emr_test_input_rgba.png")


def ensure_rgba_image():
    """sam3d 용 RGBA 입력. 알파 = 마스크(프론트가 보내는 모양). fal 경로의 마스크 추출을 실제로 태운다."""
    if os.path.exists(IMG_RGBA):
        return
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (256, 256), (200, 200, 205, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([32, 104, 224, 208], radius=24, fill=(120, 85, 60, 255))
    d.rounded_rectangle([32, 64, 224, 136], radius=24, fill=(145, 105, 75, 255))
    im.save(IMG_RGBA)
    print(f"[준비] RGBA 테스트 이미지 생성: {IMG_RGBA}")


def sam3d_mode():
    mode = os.getenv("E2E_SAM3D", "auto").strip().lower()
    if mode == "auto":
        mode = os.getenv("SAM3D_BACKEND", "local").strip().lower() or "local"
    if mode not in ("local", "fal", "skip"):
        sys.exit(f"E2E_SAM3D/SAM3D_BACKEND 는 local|fal|skip 이어야 한다: {mode!r}")
    return mode


def build_jobs(mode):
    """(job_type, params). 순서는 sam3d → room_layout → omni3d."""
    jobs = []
    if mode == "local":
        # 1회차 콜드, 2회차 웜(파이프라인 캐시), 3회차 fd 누적 확인용
        jobs += [("sam3d_mesh", {"category": "소파"})] * 3
    elif mode == "fal":
        jobs += [("sam3d_mesh", {"category": "소파"})] * int(os.getenv("E2E_SAM3D_RUNS", "1"))
    jobs += [("room_layout", {"camera_height_m": "1.6"}),
             ("omni3d", {"bbox": "10,10,54,54", "category": "액자"})]
    return jobs


def check_sam3d_payload(payload, mode):
    """(ok, 설명). sam3d 결과 JSON 이 **프론트가 실제로 읽는 모양**인지 본다.

    필수로 보는 것은 생산자(sam3d_runner.generate / sam3d_fal.convert_glb)가 항상 내는 필드이고 소비자
    (Interior3DStep.jsx 의 data.success / data.type / raw.vertices·faces / uvs·textureB64 또는 colors)가
    실제로 읽는 것뿐이다. 선택 필드는 필수로 가정하지 않는다:
      - sam3d_size_m : fal 은 SAM3D_FAL_SIZE_MODE=none 이면 null 이다 → 없거나 null 이어도 된다
      - backend      : fal 결과에만 붙는다(우리 표식). local 결과에는 없다
    """
    if not isinstance(payload, dict):
        return False, "결과가 JSON 객체가 아니다"
    if payload.get("success") is not True:
        return False, f"success 가 True 가 아니다 ({payload.get('success')!r}, error={payload.get('error')!r})"
    typ = payload.get("type")
    if typ not in ("textured", "vertex_color"):
        return False, f"type 이 textured|vertex_color 가 아니다 ({typ!r})"
    m = payload.get("mesh")
    if not isinstance(m, dict):
        return False, "mesh 가 객체가 아니다"
    v, f = m.get("vertices"), m.get("faces")

    def rows_ok(a, width):
        return isinstance(a, list) and len(a) > 0 and isinstance(a[0], list) and len(a[0]) == width

    if not rows_ok(v, 3):
        return False, "mesh.vertices 가 비었거나 [x,y,z] 행이 아니다"
    if not rows_ok(f, 3):
        return False, "mesh.faces 가 비었거나 [a,b,c] 행이 아니다"
    if typ == "textured":
        uvs, tex = m.get("uvs"), m.get("textureB64")
        if not rows_ok(uvs, 2) or len(uvs) != len(v):
            return False, "textured 인데 mesh.uvs 가 비었거나 정점 수와 다르다"
        if not isinstance(tex, str) or not tex:
            return False, "textured 인데 mesh.textureB64 가 비었다"
    else:
        c = m.get("colors")
        if not rows_ok(c, 3) or len(c) != len(v):
            return False, "vertex_color 인데 mesh.colors 가 비었거나 정점 수와 다르다"
    sz = payload.get("sam3d_size_m")
    if sz is not None and not (isinstance(sz, dict) and all(isinstance(sz.get(k), (int, float))
                                                          for k in ("width", "height", "depth"))):
        return False, f"sam3d_size_m 이 null 도 아니고 {{width,height,depth}} 도 아니다 ({sz!r})"
    if mode == "fal" and payload.get("backend") != "fal":
        return False, f"fal 모드인데 결과의 backend={payload.get('backend')!r} — 로컬 경로로 돌았다"
    if mode == "local" and payload.get("backend") == "fal":
        return False, "local 모드인데 결과가 fal 이다"
    return True, f"backend={payload.get('backend', 'local')} type={typ} v={len(v)} f={len(f)}"


def validate_done(res, get):
    """status==done 응답의 **API 계약** 검증. (ok, payload|None, 설명)

    GET /api/jobs/{id} 는 done 일 때 항상 result_url(S3 presigned)을 낸다(deploy/api/main.py get_job).
    그래서 done 인데 result_url 이 없거나, 받은 응답이 200 이 아니거나, JSON 객체가 아니면 실패다.
    get 은 requests.get 같은 호출 가능 객체(테스트에서 가짜로 바꾼다).
    """
    url = res.get("result_url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False, None, f"done 인데 result_url 이 없거나 URL 이 아니다: {url!r}"
    try:
        rr = get(url)
    except Exception as e:
        return False, None, f"결과 다운로드 실패: {type(e).__name__}: {e}"
    if rr.status_code != 200:
        return False, None, f"결과 다운로드 HTTP {rr.status_code}"
    try:
        payload = rr.json()
    except Exception as e:
        return False, None, f"결과 JSON 파싱 실패: {e} (본문 {len(rr.content)}B)"
    if not isinstance(payload, dict):
        return False, None, "결과 JSON 이 객체가 아니다"
    return True, payload, f"keys={sorted(payload.keys())}"


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

def submit(job_type, params, img=None):
    with open(img or IMG, "rb") as f:
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

def non_retryable_regression():
    """
    ③ 회귀: 잘못된 파라미터는 **재시도 없이** 즉시 실패하고 큐에서 사라진다.

    두 갈래를 다 본다.
      ① API 가 제출 시점에 400 으로 막는가      (큐에 아예 안 들어간다)
      ② 큐에 직접 넣으면 워커가 즉시 지우는가   (옛 메시지·다른 경로 대비)

    ②가 중요한 이유: 지우지 않으면 가시성 120초 x 3회 동안 백로그 메트릭이
    0 이 아니고, 그동안 오토스케일링이 GPU 를 깨운다. 지난 부하 테스트에서
    테스트가 끝난 뒤 인스턴스가 한 대 더 뜬 게 이것 때문이었다.

    input_key 를 **없는 키**로 둔 건 일부러다. 검증이 다운로드보다 앞서지
    않으면 S3 404(= retryable)가 먼저 나서 failure_kind 가 달라진다.
    즉 이 테스트는 "검증이 앞단에 있다"까지 같이 증명한다.
    """
    import uuid
    import boto3

    print("\n=== ③ non-retryable 회귀 ===")
    ep = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566") or None
    region = os.getenv("AWS_REGION", "ap-northeast-2")
    sqs = boto3.client("sqs", endpoint_url=ep, region_name=region)
    ddb = boto3.resource("dynamodb", endpoint_url=ep, region_name=region)
    table = ddb.Table(os.getenv("DDB_TABLE", "emr-jobs"))
    qname = os.getenv("Q_SCENE", "emr-scene")
    qurl = sqs.get_queue_url(QueueName=qname)["QueueUrl"]

    # ① API 가 막는가
    with open(IMG, "rb") as f:
        r = requests.post(f"{API}/api/jobs",
                          files={"image": ("in.png", f.read(), "image/png")},
                          data={"job_type": "omni3d"}, timeout=30)
    api_ok = r.status_code == 400
    print(f"    ① API 거절: {r.status_code} {'OK' if api_ok else 'FAIL(400 이어야 한다)'}")
    if not api_ok:
        print(f"       본문: {r.text[:200]}")

    # ② 큐에 직접 넣으면 워커가 즉시 지우는가
    jid = uuid.uuid4().hex
    now = int(time.time())
    msg = {"job_id": jid, "job_type": "omni3d",
           "input_key": "input/없는키/in.png",
           "input_keys": {"image": "input/없는키/in.png"}, "params": {}}
    table.put_item(Item={**msg, "status": "queued", "created_at": now,
                         "updated_at": now, "expires_at": now + 3600})
    sqs.send_message(QueueUrl=qurl, MessageBody=json.dumps(msg))
    print(f"    ② 큐 직접 주입 job_id={jid}")

    res, el = poll(jid, timeout=120)
    kind = res.get("failure_kind")
    kind_ok = res.get("status") == "failed" and kind == "non_retryable"
    print(f"       상태={res.get('status')} 분류={kind} ({el:.1f}초)"
          f" {'OK' if kind_ok else 'FAIL'}")

    # 삭제됐는지 — 큐가 비워질 때까지 본다. 재시도 창(약 240초)보다 훨씬 짧은
    # 60초 안에 0 이 되어야 "즉시 지웠다"고 말할 수 있다.
    drained, t0 = False, time.time()
    while time.time() - t0 < 60:
        a = sqs.get_queue_attributes(
            QueueUrl=qurl,
            AttributeNames=["ApproximateNumberOfMessages",
                            "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        depth = int(a["ApproximateNumberOfMessages"]) + \
            int(a["ApproximateNumberOfMessagesNotVisible"])
        if depth == 0:
            drained = True
            break
        time.sleep(2)
    print(f"       큐 비움: {'OK' if drained else 'FAIL(메시지가 남아 재시도된다)'}"
          f" ({time.time()-t0:.1f}초)")

    return api_ok and kind_ok and drained


ensure_image()
ensure_rgba_image()
MODE = sam3d_mode()
JOBS = build_jobs(MODE)
print(f"[e2e] SAM3D 모드={MODE}, 작업 {len(JOBS)}건"
      + (f" (fal 호출 {sum(1 for j in JOBS if j[0]=='sam3d_mesh')}회 — 요금 발생)" if MODE == "fal" else ""))

# 구간 표시(측정 중일 때만 의미가 있다). 측정이 안 켜져 있으면 조용히 건너뛴다.
def mark(label):
    try:
        subprocess.run(["bash", os.path.join(REPO_ROOT, "deploy/aws/measure-resources.sh"), "mark", label],
                       capture_output=True, timeout=10)
    except Exception:
        pass

results = []
n_sam3d = 0
for i, (jt, params) in enumerate(JOBS, 1):
    if jt == "sam3d_mesh":
        n_sam3d += 1
    label = f"{jt}{' (fal)' if jt=='sam3d_mesh' and MODE=='fal' else ' (웜)' if jt=='sam3d_mesh' and n_sam3d==2 else ' (콜드)' if jt=='sam3d_mesh' else ''}"
    print(f"\n=== {i}. {label} ===")
    mark(f"{i}-{jt}")
    jid = submit(jt, params, img=IMG_RGBA if jt == "sam3d_mesh" else None)
    print(f"    job_id={jid}")
    res, el = poll(jid)
    ok = res.get("status") == "done"
    detail = ""
    if ok:
        # 모든 작업: done → result_url 있음 + 다운로드 200 + JSON 객체(API 계약). 없으면 실패다.
        ok, payload, detail = validate_done(res, lambda u: requests.get(u, timeout=60))
        if ok and jt == "sam3d_mesh":          # skip 모드에는 sam3d 작업이 아예 없다
            p_ok, p_detail = check_sam3d_payload(payload, MODE)
            detail += f" | {p_detail}"
            ok = p_ok
        elif ok and isinstance(payload.get("mesh"), dict):
            m = payload["mesh"]
            detail += f" verts={len(m.get('vertices',[]))} faces={len(m.get('faces',[]))}"
    else:
        detail = json.dumps(res, ensure_ascii=False)[:300]
    fds = worker_fds() if jt == "sam3d_mesh" and MODE == "local" else None  # fd 누적은 로컬 GPU 경로의 문제다
    results.append((label, ok, el, detail, fds))
    print(f"    -> {'OK' if ok else 'FAIL'} {el:.1f}s  {detail}"
          + (f"\n       워커 fd 수: {fds}" if fds is not None else ""))

nr_ok = non_retryable_regression()
results.append(("③ non-retryable", nr_ok, 0.0, "", None))

print("\n" + "="*70)
print(f"{'작업':<22}{'결과':<8}{'소요':>8}")
print("-"*70)
for label, ok, el, _, fds in results:
    print(f"{label:<22}{'OK' if ok else 'FAIL':<8}{el:>7.1f}s"
          + (f"   fd={fds}" if fds not in (None, -1) else ""))
print("="*70)
sys.exit(0 if all(r[1] for r in results) else 1)
