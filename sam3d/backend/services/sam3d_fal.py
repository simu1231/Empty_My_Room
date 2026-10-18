"""
SAM3D 메쉬 생성 — fal.ai(`fal-ai/sam-3/3d-objects`) 백엔드.

sam3d_runner.run_sam3d()가 SAM3D_BACKEND=fal 일 때만 이 모듈을 부른다. 반환하는
dict 모양은 sam3d_runner.generate()와 같아서 프론트(Interior3DStep.jsx)는 어느
쪽이 만든 결과인지 모른다.

로컬 경로와 다른 점 (조사 결과 A~H 중 이 모듈이 다루는 것)
────────────────────────────────────────────────────────
A. 출력이 JSON 배열이 아니라 GLB URL이다 → convert_glb()가 dict로 바꾼다.
B. 마스크를 알파 채널로 못 넘긴다 → prepare_inputs()가 RGB와 마스크 PNG로 나눈다.
C. 전처리(크롭/회색 배경/리사이즈)는 로컬과 같은 함수를 로컬에서 먼저 돌린다.
D. seed 탐색은 하지 않는다 → seed 고정(SAM3D_FAL_SEED, 기본 5) 1회 호출. 품질 검사는
   로그만 남긴다. 재시도(SAM3D_FAL_MAX_RETRIES)는 기본 0 = 꺼짐.
E. sam3d_size_m: fal metadata의 scale 의미가 문서에 없다 → 기본은 None(프론트가
   방 크기 폴백으로 넘어감). 요청마다 남는 [fal-diag] 로그를 보고 SAM3D_FAL_SIZE_MODE 로 켠다.
F. 좌표계: fal GLB 는 upstream sam-3d-objects 의 Y-up 출력이고, 우리 로컬 출력은
   sam3d_patches/.../postprocessing_utils.py 의 rot=[[1,0,0],[0,0,1],[0,-1,0]]
   (커밋 f7a2cef "방향 버그 수정") 때문에 upstream 을 X축 기준 180° 돌린 것과 같다.
   프론트는 그 좌표계를 가정하므로 기본 SAM3D_FAL_AXIS=x,-y,-z 를 켠다.
G. 납작한 카테고리 z 부풀리기는 같은 규칙을 변환 뒤에 적용한다(shape prior는 미적용).
H. 버텍스 컬러 보정(×1.15, γ0.85)은 로컬과 같게 적용한다.

환경변수
────────
  FAL_KEY                        fal API 키(필수). 없으면 저장소 루트/backend 의 .env 를 읽는다.
  SAM3D_FAL_MODEL                기본 fal-ai/sam-3/3d-objects
  SAM3D_FAL_SEED                 고정 seed, 기본 5
  SAM3D_FAL_MAX_RETRIES          품질 미달 시 다른 seed 로 재호출할 최대 횟수, 기본 0(꺼짐)
  SAM3D_FAL_MIN_THICKNESS_RATIO  품질 검사: 최소/최대 extent 비율 하한, 기본 0.08 (로그용)
  SAM3D_FAL_MIN_VERTICES         품질 검사: 정점 수 하한, 기본 500 (로그용)
  SAM3D_FAL_AXIS                 'none' 또는 'x,-y,-z' 꼴(새 x,y,z 를 옛 축으로 표현), 기본 x,-y,-z
  SAM3D_FAL_SIZE_MODE            none | glb_extent | extent_x_scale, 기본 none
  SAM3D_FAL_TIMEOUT              fal 호출 전체 제한(초), 기본 120
  SAM3D_FAL_PROMPT               비어 있으면 prompt 를 보내지 않는다(기본). 값이 있으면 그대로 보낸다
  SAM3D_FAL_DEPS                 fal_client 가 import 안 될 때 sys.path 끝에 붙일 디렉터리, 기본 /opt/fal-deps

마스크는 항상 직접 넘긴다(입력 RGBA 의 알파 → 마스크 PNG). fal 스키마상 prompt/point/box
프롬프트는 "마스크가 없을 때의 자동 분할용"이라 보내지 않는다. 마스크 포맷(흰색=물체)은
문서에 없는 관례 가정이라 첫 실호출 결과로 확인해야 한다.

품질 검사 기본값은 **아직 보정되지 않은 추정치**다. [fal-quality] 로그의 불합격 비율을 보고 조정할 것.
"""
import io
import os
import time
import base64
import random
import threading
import urllib.request
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from services.sam3d_runner import _crop_and_resize, FLAT_CATEGORIES, THIN_CATEGORIES

FAL_MODEL_DEFAULT = 'fal-ai/sam-3/3d-objects'
# fal(upstream) Y-up → 로컬 출력 좌표계(프론트가 가정). 180° X 회전이라 거울상이 아니다(det=+1).
DEFAULT_AXIS = 'x,-y,-z'


# ── 설정 ────────────────────────────────────────────────────────────────

def _load_dotenv_once():
    """FAL_KEY가 없으면 .env 에서 읽는다. 이 프로젝트에는 dotenv 로더가 없어서 직접 파싱한다."""
    if os.environ.get('FAL_KEY'):
        return
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.dirname(here), os.path.dirname(os.path.dirname(here)),
              os.path.dirname(os.path.dirname(os.path.dirname(here)))):
        path = os.path.join(d, '.env')
        if not os.path.isfile(path):
            continue
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k = k.removeprefix('export ').strip()
                if k == 'FAL_KEY':
                    os.environ['FAL_KEY'] = v.strip().strip('"').strip("'")
                    return


def _cfg_int(name, default):
    return int(os.environ.get(name, default))


def _cfg_float(name, default):
    return float(os.environ.get(name, default))


def _require_key():
    _load_dotenv_once()
    if not os.environ.get('FAL_KEY'):
        raise RuntimeError('FAL_KEY 환경변수가 없다 (SAM3D_BACKEND=fal). .env 또는 환경변수로 설정할 것.')


def _import_fal_client():
    """fal_client import. 마운트된 conda 환경에 없으면 이미지의 /opt/fal-deps 를 **끝에** 붙여 다시 시도한다.

    PYTHONPATH 로 앞에 얹으면 환경이 검증해 둔 httpx 등을 가리므로 끝에 붙인다(gpu/Dockerfile 참고).
    """
    try:
        import fal_client
        return fal_client
    except ImportError:
        import sys
        d = os.environ.get('SAM3D_FAL_DEPS', '/opt/fal-deps')
        if os.path.isdir(d) and d not in sys.path:
            sys.path.append(d)
        import fal_client
        return fal_client


def _scrub(text: str) -> str:
    """로그/에러 문자열에서 키가 새지 않게 한다."""
    key = os.environ.get('FAL_KEY', '')
    return text.replace(key, '***') if key and len(key) >= 8 else text


class FalCallError(RuntimeError):
    """fal 호출 실패. 입력 문제(400/422)일 때만 .response 를 달아 워커가 재시도 없이 버리게 한다."""


# ── 입력 ────────────────────────────────────────────────────────────────

def _png_data_uri(arr: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


def prepare_inputs(img_bytes: bytes):
    """RGBA(알파=마스크) → (image data URI, mask data URI). 전처리는 로컬 generate()와 같다."""
    rgba = np.array(Image.open(io.BytesIO(img_bytes)).convert('RGBA'))
    mask = rgba[..., 3] > 127
    rgb = rgba[..., :3]
    rgb, mask = _crop_and_resize(rgb, mask)
    rgb = rgb.copy()
    rgb[~mask] = 128
    return _png_data_uri(rgb), _png_data_uri(mask.astype(np.uint8) * 255)


# ── fal 호출 ────────────────────────────────────────────────────────────

@dataclass
class FalRaw:
    glb: bytes
    metadata: list
    seed: int
    request_id: str = ''
    t_total: float = 0.0   # 제출 → GLB 다운로드 끝
    t_queue: float = 0.0   # 제출 → 실행 시작(InProgress). 콜드스타트/대기열이 여기에 보인다
    t_infer: float = 0.0   # 실행 시작 → 완료
    t_download: float = 0.0
    result_keys: list = field(default_factory=list)


def fal_generate_raw(img_bytes: bytes, seed: int, image_uri=None, mask_uri=None) -> FalRaw:
    """fal 큐에 1건 제출하고 끝날 때까지 기다린다(subscribe). GLB 바이트와 metadata를 그대로 돌려준다."""
    _require_key()
    fal_client = _import_fal_client()

    if image_uri is None or mask_uri is None:
        image_uri, mask_uri = prepare_inputs(img_bytes)

    model = os.environ.get('SAM3D_FAL_MODEL', FAL_MODEL_DEFAULT)
    timeout = _cfg_float('SAM3D_FAL_TIMEOUT', 120)
    marks = {}

    def on_enqueue(request_id):
        marks['id'] = request_id

    def on_update(status):
        # Queued → InProgress → Completed. 처음 InProgress 를 본 시각만 기록한다.
        if isinstance(status, fal_client.InProgress) and 'run' not in marks:
            marks['run'] = time.time()

    arguments = {
        'image_url': image_uri,
        'mask_urls': [mask_uri],
        'seed': int(seed),
        'export_textured_glb': True,   # 항상 true — 텍스처 GLB 를 받아 textured 경로로 변환한다
    }
    prompt = os.environ.get('SAM3D_FAL_PROMPT', '').strip()
    if prompt:
        arguments['prompt'] = prompt

    t0 = time.time()
    result = fal_client.subscribe(
        model,
        arguments=arguments,
        on_enqueue=on_enqueue,
        on_queue_update=on_update,
        client_timeout=timeout,
    )
    t_done = time.time()

    glb_file = result.get('model_glb') or {}
    url = glb_file.get('url')
    if not url:
        raise RuntimeError(f'fal 응답에 model_glb 가 없다: keys={list(result.keys())}')
    t1 = time.time()
    with urllib.request.urlopen(url, timeout=60) as r:
        glb = r.read()
    t2 = time.time()

    t_run = marks.get('run', t0)
    return FalRaw(
        glb=glb,
        metadata=result.get('metadata') or [],
        seed=int(seed),
        request_id=marks.get('id', ''),
        t_total=t2 - t0,
        t_queue=max(0.0, t_run - t0),
        t_infer=max(0.0, t_done - t_run),
        t_download=t2 - t1,
        result_keys=list(result.keys()),
    )


# ── 변환 (GLB → 프론트가 읽는 dict) ──────────────────────────────────────

def _parse_axis(spec: str):
    """'x,-z,y' → 3x3 행렬 R. 새 좌표 = R @ 옛 좌표. 'none'/빈 문자열이면 None."""
    spec = (spec or '').strip().lower()
    if spec in ('', 'none'):
        return None
    idx = {'x': 0, 'y': 1, 'z': 2}
    parts = [p.strip() for p in spec.split(',')]
    if len(parts) != 3:
        raise ValueError(f'SAM3D_FAL_AXIS 형식 오류: {spec!r} (예: x,-z,y)')
    R = np.zeros((3, 3), dtype=np.float32)
    for i, p in enumerate(parts):
        sign = -1.0 if p.startswith('-') else 1.0
        R[i, idx[p.lstrip('+-')]] = sign
    return R


def axis_spec_from_matrix(R: np.ndarray) -> str:
    """_parse_axis 의 역. 비교 스크립트가 찾은 회전을 환경변수 값으로 출력할 때 쓴다."""
    names = 'xyz'
    out = []
    for row in np.round(R).astype(int):
        j = int(np.argmax(np.abs(row)))
        out.append(('-' if row[j] < 0 else '') + names[j])
    return ','.join(out)


def _load_mesh(glb_bytes: bytes):
    import trimesh
    loaded = trimesh.load(io.BytesIO(glb_bytes), file_type='glb')
    if isinstance(loaded, trimesh.Scene):
        meshes = [g for g in loaded.dump() if isinstance(g, trimesh.Trimesh)]  # 노드 변환 적용
        if not meshes:
            raise RuntimeError('GLB 안에 메시가 없다')
        return meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
    return loaded


def _scale_vec(metadata):
    if not metadata:
        return None
    s = metadata[0].get('scale')
    if s is None:
        return None
    s = np.asarray(s, dtype=np.float64).reshape(-1)
    if s.size == 1:
        s = np.repeat(s, 3)
    return s[:3] if s.size >= 3 else None


def convert_glb(raw: FalRaw, category: str = '', diag: dict = None) -> dict:
    """fal GLB → sam3d_runner.generate() 와 같은 모양의 dict. diag 를 주면 진단값을 채운다."""
    mesh = _load_mesh(raw.glb)
    vertices = np.array(mesh.vertices, dtype=np.float32)
    faces = np.array(mesh.faces, dtype=np.int32)
    if diag is not None:
        diag['raw_extent'] = (vertices.max(axis=0) - vertices.min(axis=0)).tolist()
        diag['raw_y'] = (float(vertices[:, 1].min()), float(vertices[:, 1].max()))

    R = _parse_axis(os.environ.get('SAM3D_FAL_AXIS', DEFAULT_AXIS))
    if R is not None:
        vertices = vertices @ R.T

    # 납작한 카테고리 → z축 강제 부풀리기 (sam3d_runner.generate 와 같은 규칙)
    if any(c in category for c in FLAT_CATEGORIES | THIN_CATEGORIES):
        ranges = vertices.max(axis=0) - vertices.min(axis=0)
        min_range = ranges.min()
        flat_axis = int(ranges.argmin())
        target = max(ranges.max() * 0.25, 0.05)
        if min_range < target:
            center = (vertices[:, flat_axis].max() + vertices[:, flat_axis].min()) / 2
            factor = target / max(min_range, 1e-6)
            vertices[:, flat_axis] = center + (vertices[:, flat_axis] - center) * factor

    extents = vertices.max(axis=0) - vertices.min(axis=0)
    size_mode = os.environ.get('SAM3D_FAL_SIZE_MODE', 'none')
    sam3d_size_m = None
    if size_mode == 'glb_extent':
        sam3d_size_m = {'width': float(extents[0]), 'height': float(extents[1]), 'depth': float(extents[2])}
    elif size_mode == 'extent_x_scale':
        sv = _scale_vec(raw.metadata)
        if sv is not None:
            e = extents * sv
            sam3d_size_m = {'width': float(e[0]), 'height': float(e[1]), 'depth': float(e[2])}

    vis = mesh.visual
    uv = getattr(vis, 'uv', None)
    mat = getattr(vis, 'material', None)
    tex = getattr(mat, 'baseColorTexture', None) if mat is not None else None
    if tex is None and mat is not None:
        tex = getattr(mat, 'image', None)

    if uv is not None and tex is not None:
        buf = io.BytesIO()
        tex.convert('RGB').save(buf, format='JPEG', quality=90)
        return {
            'success': True,
            'type': 'textured',
            'backend': 'fal',
            'sam3d_size_m': sam3d_size_m,
            'mesh': {
                'vertices': vertices.tolist(),
                'faces': faces.tolist(),
                'uvs': np.array(uv, dtype=np.float32).tolist(),
                'textureB64': base64.b64encode(buf.getvalue()).decode(),
            },
        }

    vc = getattr(vis, 'vertex_colors', None)
    if vc is not None and len(vc) == len(vertices):
        colors = np.array(vc[:, :3], dtype=np.float32) / 255.0
    else:
        colors = np.full((len(vertices), 3), 0.8, dtype=np.float32)
    colors = np.clip(colors * 1.15, 0, 1)
    colors = np.power(colors, 0.85).astype(np.float32)
    return {
        'success': True,
        'type': 'vertex_color',
        'backend': 'fal',
        'sam3d_size_m': sam3d_size_m,
        'mesh': {
            'vertices': vertices.tolist(),
            'faces': faces.tolist(),
            'colors': colors.tolist(),
        },
    }


# ── 품질 검사 ───────────────────────────────────────────────────────────

def mesh_metrics(payload: dict) -> dict:
    """정점/면 수, extent, 두께 비율(최소 extent / 최대 extent)."""
    v = np.asarray(payload['mesh']['vertices'], dtype=np.float32)
    ext = v.max(axis=0) - v.min(axis=0)
    return {
        'n_vertices': int(len(v)),
        'n_faces': int(len(payload['mesh']['faces'])),
        'extent': ext.tolist(),
        'thickness_ratio': float(ext.min() / max(ext.max(), 1e-9)),
    }


def check_quality(payload: dict, category: str = ''):
    """(통과 여부, 사유). 납작/얇은 카테고리는 어차피 부풀리므로 두께 비율은 보지 않는다."""
    m = mesh_metrics(payload)
    if m['n_faces'] == 0 or m['n_vertices'] < _cfg_int('SAM3D_FAL_MIN_VERTICES', 500):
        return False, f"정점/면 부족 (v={m['n_vertices']}, f={m['n_faces']})"
    if not np.isfinite(m['extent']).all() or max(m['extent']) <= 0:
        return False, f"extent 이상 {m['extent']}"
    if not any(c in category for c in FLAT_CATEGORIES | THIN_CATEGORIES):
        lo = _cfg_float('SAM3D_FAL_MIN_THICKNESS_RATIO', 0.08)
        if m['thickness_ratio'] < lo:
            return False, f"두께 비율 {m['thickness_ratio']:.3f} < {lo}"
    return True, 'ok'


# ── 호출 통계 ───────────────────────────────────────────────────────────

_stats_lock = threading.Lock()
_stats = {'requests': 0, 'fal_calls': 0, 'failed_calls': 0, 'failed_requests': 0,
          'quality_fail_requests': 0, 'retried_requests': 0, 'total_seconds': 0.0}


def stats_snapshot() -> dict:
    with _stats_lock:
        s = dict(_stats)
    ok = s['requests'] - s['failed_requests']
    s['quality_fail_rate'] = s['quality_fail_requests'] / ok if ok else 0.0
    s['retry_rate'] = s['retried_requests'] / s['requests'] if s['requests'] else 0.0
    return s


def _fmt_stats() -> str:
    s = stats_snapshot()
    ok = s['requests'] - s['failed_requests']
    avg = s['total_seconds'] / s['fal_calls'] if s['fal_calls'] else 0.0
    return (f"요청 {s['requests']}건 / fal 호출 {s['fal_calls']}회(평균 {avg:.1f}초) / 호출 실패 {s['failed_calls']}회 "
            f"/ 요청 실패 {s['failed_requests']}건 / 품질 불합격 {s['quality_fail_requests']}/{ok}건 "
            f"({s['quality_fail_rate'] * 100:.0f}%) / 재시도 요청 {s['retried_requests']}건")


# ── 진입점 ──────────────────────────────────────────────────────────────

def _log_diag(raw: FalRaw, diag: dict, payload: dict):
    """방향/크기 판단용 로그. 요청마다 한 줄. 이 값으로 SAM3D_FAL_AXIS / SAM3D_FAL_SIZE_MODE 를 정한다."""
    def r(x, n=4):
        return np.round(np.asarray(x, dtype=np.float64), n).tolist()
    fin = np.asarray(payload['mesh']['vertices'], dtype=np.float32)
    ext = fin.max(axis=0) - fin.min(axis=0)
    md = raw.metadata[0] if raw.metadata else {}
    print(f"[fal-diag] 원본GLB extent(xyz)={r(diag.get('raw_extent', []))} Y[min,max]={r(diag.get('raw_y', []))}")
    print(f"[fal-diag] 변환후({os.environ.get('SAM3D_FAL_AXIS', DEFAULT_AXIS)}) extent(xyz)={r(ext)} "
          f"Y[min,max]={r([fin[:, 1].min(), fin[:, 1].max()])} 크기모드={os.environ.get('SAM3D_FAL_SIZE_MODE', 'none')} "
          f"sam3d_size_m={payload.get('sam3d_size_m')}")
    print(f"[fal-diag] metadata scale={md.get('scale')} rotation={md.get('rotation')} "
          f"translation={md.get('translation')} (keys={sorted(md.keys())})")


def generate_fal(img_bytes: bytes, category: str = '') -> dict:
    """
    seed 고정(SAM3D_FAL_SEED) 1회 호출. 품질 검사는 로그만 남긴다.
    SAM3D_FAL_MAX_RETRIES>0 이면 불합격일 때만 다른 seed 로 재호출한다(기본 꺼짐).
    호출이 실패하면 FalCallError 를 던진다 — fal 모드에는 로컬 폴백이 없다.
    """
    max_retries = _cfg_int('SAM3D_FAL_MAX_RETRIES', 0)
    fixed_seed = _cfg_int('SAM3D_FAL_SEED', 5)
    image_uri, mask_uri = prepare_inputs(img_bytes)  # 재시도에서 전처리를 반복하지 않는다

    best, best_ratio, calls, reasons, last_ok, last_why = None, -1.0, 0, [], True, 'ok'
    with _stats_lock:
        _stats['requests'] += 1
    for attempt in range(max_retries + 1):
        seed = fixed_seed if attempt == 0 else random.randint(0, 2**31)
        calls += 1
        t0 = time.time()
        try:
            raw = fal_generate_raw(img_bytes, seed, image_uri, mask_uri)
            diag = {}
            payload = convert_glb(raw, category, diag)
        except Exception as e:
            dt = time.time() - t0
            msg = _scrub(f'{type(e).__name__}: {e}')
            with _stats_lock:
                _stats['fal_calls'] += 1
                _stats['failed_calls'] += 1
                _stats['total_seconds'] += dt
                _stats['failed_requests'] += 1
                if attempt > 0:
                    _stats['retried_requests'] += 1
            print(f"[fal] 실패 seed={seed} {dt:.1f}초 ({msg})")
            print(f"[fal] {_fmt_stats()}")
            err = FalCallError(f'fal 호출 실패: {msg}')
            resp = getattr(e, 'response', None)
            if getattr(resp, 'status_code', None) in (400, 422):
                err.response = resp   # jobspec.is_non_retryable: 입력 문제는 재시도해도 같다
            raise err from None
        with _stats_lock:
            _stats['fal_calls'] += 1
            _stats['total_seconds'] += raw.t_total
        _log_diag(raw, diag, payload)
        ok, why = check_quality(payload, category)
        m = mesh_metrics(payload)
        print(f"[fal] 시도 {attempt + 1}/{max_retries + 1} seed={seed} {raw.t_total:.1f}초"
              f"(대기 {raw.t_queue:.1f}/실행 {raw.t_infer:.1f}/다운로드 {raw.t_download:.1f}) "
              f"v={m['n_vertices']} f={m['n_faces']} 두께비={m['thickness_ratio']:.3f}")
        print(f"[fal-quality] {'PASS' if ok else 'FAIL'} category={category!r} seed={seed} {why}")
        last_ok, last_why = ok, why
        if m['thickness_ratio'] > best_ratio:
            best, best_ratio = payload, m['thickness_ratio']
        if ok:
            break
        reasons.append(why)
    if calls > 1 and not last_ok:
        print(f"[fal] 재시도 소진, 가장 입체적인 결과 사용 (사유: {reasons})")

    with _stats_lock:
        if calls > 1:
            _stats['retried_requests'] += 1
        if not last_ok:
            _stats['quality_fail_requests'] += 1
    print(f"[fal] {_fmt_stats()}")
    return best
