import io
import gc
import torch
import numpy as np
import orjson
from PIL import Image
from fastapi import APIRouter, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import Response

router = APIRouter()

PIPELINE_CONFIG = '/home/tmvlem5671/sam-3d-objects/checkpoints/hf/checkpoints/pipeline.yaml'
WORKSPACE_DIR   = '/home/tmvlem5671/sam-3d-objects/checkpoints/hf/checkpoints'

CACHE_SAM3D_PIPELINE = True
MIN_INPUT_SIZE = 512   # 너무 작은 이미지는 업스케일 (깊이 추정 정확도)
MAX_INPUT_SIZE = 1024  # 너무 큰 이미지는 다운스케일 (속도)
TARGET_FACES   = 80000

# utils3d 1.7 함수명 변경 호환 패치
import utils3d.torch as _u3d
from utils3d.torch.transforms import intrinsics_from_fov as _intr_fov, perspective_from_fov as _persp_fov
from utils3d.torch.mesh import (
    mesh_connected_components  as _mesh_cc,
    mesh_dual_graph            as _mesh_dg,
    mesh_edges                 as _mesh_edges,
    graph_connected_components as _graph_cc,
    remove_unused_vertices     as _rm_unused,
)
if not hasattr(_u3d, 'intrinsics_from_fov_xy'):
    _u3d.intrinsics_from_fov_xy = lambda fx, fy: _intr_fov(fov_x=fx, fov_y=fy)
if not hasattr(_u3d, 'perspective_from_fov_xy'):
    _u3d.perspective_from_fov_xy = lambda fx, fy, near, far: _persp_fov(fov_x=fx, fov_y=fy, near=near, far=far)
if not hasattr(_u3d, 'compute_connected_components'):
    _u3d.compute_connected_components = _mesh_cc
if not hasattr(_u3d, 'compute_dual_graph'):
    _u3d.compute_dual_graph = _mesh_dg
if not hasattr(_u3d, 'compute_edges'):
    _u3d.compute_edges = _mesh_edges
if not hasattr(_u3d, 'compute_edge_connected_components'):
    _u3d.compute_edge_connected_components = _graph_cc
if not hasattr(_u3d, 'remove_unreferenced_vertices'):
    _u3d.remove_unreferenced_vertices = _rm_unused


def _crop_and_resize(rgb: np.ndarray, mask: np.ndarray):
    """마스크 bbox 크롭 후 MIN~MAX 범위로 리사이즈 (소형 이미지 업스케일 포함)."""
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if not rows.any() or not cols.any():
        return rgb, mask

    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]
    pad = 8
    rmin = max(0, rmin - pad)
    rmax = min(rgb.shape[0], rmax + pad + 1)
    cmin = max(0, cmin - pad)
    cmax = min(rgb.shape[1], cmax + pad + 1)

    rgb  = rgb[rmin:rmax, cmin:cmax]
    mask = mask[rmin:rmax, cmin:cmax]

    h, w = rgb.shape[:2]
    cur_max = max(h, w)

    if cur_max < MIN_INPUT_SIZE:
        # 작은 이미지 업스케일 → 깊이 추정 품질 향상
        scale = MIN_INPUT_SIZE / cur_max
    elif cur_max > MAX_INPUT_SIZE:
        # 큰 이미지 다운스케일 → 속도
        scale = MAX_INPUT_SIZE / cur_max
    else:
        scale = 1.0

    if scale != 1.0:
        new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
        rgb  = np.array(Image.fromarray(rgb).resize((new_w, new_h), Image.LANCZOS))
        mask = np.array(Image.fromarray(mask.astype(np.uint8) * 255).resize((new_w, new_h), Image.NEAREST)) > 127

    print(f"[전처리] {w}x{h} → {rgb.shape[1]}x{rgb.shape[0]}")
    return rgb, mask


# 납작한 물체 카테고리 → shape prior 적용
FLAT_CATEGORIES  = {'시계', '액자', '그림', '거울'}
THIN_CATEGORIES  = {'조명', '꽃', '화분'}

def _make_shape_prior(mask: np.ndarray, category: str, base_z: float = 1.5) -> 'torch.Tensor | None':
    """마스크 형태 기반 간단한 shape prior pointmap (H, W, 3)."""
    cat = category.strip()
    if not any(c in cat for c in FLAT_CATEGORIES | THIN_CATEGORIES):
        return None

    H, W = mask.shape
    rows, cols = np.where(mask)
    if len(rows) == 0:
        return None

    cy = rows.mean(); cx = cols.mean()
    half_h = max((rows.max() - rows.min()) / 2, 1)
    half_w = max((cols.max() - cols.min()) / 2, 1)
    radius = max(half_h, half_w)

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    xn = (xx - cx) / radius   # [-1, 1] 정규화
    yn = (yy - cy) / radius

    if any(c in cat for c in FLAT_CATEGORIES):
        # 원/사각형 디스크 → 가장자리가 약간 뒤로
        r = np.sqrt(xn**2 + yn**2)
        depth_offset = np.where(mask, 0.08 * (1.0 - np.clip(r, 0, 1)), 0)
    else:
        # 조명/꽃/화분 → 원뿔형 (아래쪽이 더 가까이)
        depth_offset = np.where(mask, 0.12 * np.clip(1.0 - yn * 0.5, 0, 1), 0)

    z = np.where(mask, base_z + depth_offset, np.nan).astype(np.float32)
    scale = radius / max(H, W)
    x3d = np.where(mask, xn * z * scale, np.nan).astype(np.float32)
    y3d = np.where(mask, -yn * z * scale, np.nan).astype(np.float32)

    pointmap = np.stack([x3d, y3d, z], axis=-1)  # (H, W, 3)
    print(f"[shape prior] category={cat}, base_z={base_z:.2f}")
    return torch.from_numpy(pointmap)


def _decimate_mesh(vertices: np.ndarray, faces: np.ndarray, colors: np.ndarray):
    if len(faces) <= TARGET_FACES:
        return vertices, faces, colors
    import trimesh
    from scipy.spatial import cKDTree
    m = trimesh.Trimesh(vertices=vertices, faces=faces)
    m = m.simplify_quadric_decimation(face_count=TARGET_FACES)
    new_verts  = np.asarray(m.vertices, dtype=np.float32)
    new_faces  = np.asarray(m.faces,    dtype=np.int32)
    _, idx     = cKDTree(vertices).query(new_verts)
    new_colors = colors[idx].astype(np.float32)
    print(f"[decimation] {len(faces)} → {len(new_faces)} faces")
    return new_verts, new_faces, new_colors


@router.post("/mesh")
async def generate_mesh(
    request: Request,
    image: UploadFile = File(...),
    category: str = Form(''),
):
    img_bytes = await image.read()
    image_pil = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    image_np  = np.array(image_pil)
    alpha     = image_np[..., 3]
    rgb       = image_np[..., :3]
    mask      = alpha > 127

    rgb, mask = _crop_and_resize(rgb, mask)

    # 배경을 회색으로 채움 → MoGe 깊이 추정 개선
    rgb_bg = rgb.copy()
    rgb_bg[~mask] = 128
    rgb = rgb_bg

    print("Meta SAM3D 3D 메쉬 생성 중...")

    pipeline = getattr(request.app.state, 'sam3d_pipeline', None) if CACHE_SAM3D_PIPELINE else None
    if pipeline is None:
        from omegaconf import OmegaConf
        from hydra.utils import instantiate
        config = OmegaConf.load(PIPELINE_CONFIG)
        config.rendering_engine   = 'pytorch3d'
        config.compile_model      = False
        config.workspace_dir      = WORKSPACE_DIR
        config.ss_inference_steps   = 50  # 초기 3D 포인트 밀도
        config.slat_inference_steps = 75  # 3D 잠재 구조 정확도
        config.slat_cfg_strength    = 1   # YAML 기본값
        pipeline = instantiate(config)
        if CACHE_SAM3D_PIPELINE:
            request.app.state.sam3d_pipeline = pipeline
            print("SAM3D 파이프라인 캐시 완료!")

    try:
        import random

        NUM_STAGE1_TRIES = 10

        # Stage 1만 빠르게 여러 번 → 가장 입체적인 seed 선택
        best_seed = None
        best_stage1_score = -1.0

        for attempt in range(NUM_STAGE1_TRIES):
            seed = random.randint(0, 2**31)
            r1 = pipeline.run(
                rgb, mask, seed=seed,
                stage1_only=True,
                with_mesh_postprocess=False,
                with_texture_baking=False,
                with_layout_postprocess=False,
                use_vertex_color=True,
                pointmap=None,
            )
            voxel = r1['voxel'].cpu().numpy()
            if any(c in category for c in THIN_CATEGORIES):
                # 조명/꽃/화분: 복셀 개수 많을수록 더 완전한 구조
                score = float(len(voxel))
            else:
                # 일반 가구 + 시계/액자 등: 세 축 중 가장 얇은 축 크기 (납작함 방지)
                ranges = voxel.max(axis=0) - voxel.min(axis=0)
                score = float(ranges.min())
            print(f"[Stage1 시도 {attempt+1}/{NUM_STAGE1_TRIES}] seed={seed} score={score:.4f}")
            if score > best_stage1_score:
                best_stage1_score = score
                best_seed = seed

        print(f"[최적 seed 선택] seed={best_seed} score={best_stage1_score:.4f}")

        # 최적 seed로 full 파이프라인 실행
        result = pipeline.run(
            rgb, mask, seed=best_seed,
            stage1_only=False,
            with_mesh_postprocess=False,
            with_texture_baking=False,
            with_layout_postprocess=False,
            use_vertex_color=True,
            pointmap=None,
        )

        mesh = result.get('mesh')
        if mesh is None:
            raise HTTPException(500, "메쉬 생성 실패")
        if isinstance(mesh, list):
            mesh = mesh[0]

        vertices_np = mesh.vertices.cpu().float().numpy()
        faces_np    = mesh.faces.cpu().numpy().astype(np.int32)
        colors_np   = mesh.vertex_attrs[:, :3].cpu().float().numpy() \
                      if mesh.vertex_attrs is not None \
                      else np.full((len(vertices_np), 3), 0.8, dtype=np.float32)

        # 밝기 소폭 부스트 후 가벼운 감마 보정 (낡은 느낌 방지)
        colors_np = np.clip(colors_np * 1.15, 0, 1)
        colors_np = np.power(colors_np, 0.85).astype(np.float32)

        # 납작한 카테고리 → z축 강제 부풀리기
        if any(c in category for c in FLAT_CATEGORIES | THIN_CATEGORIES):
            ranges = vertices_np.max(axis=0) - vertices_np.min(axis=0)
            min_range = ranges.min()
            flat_axis = int(ranges.argmin())
            target = max(ranges.max() * 0.25, 0.05)
            if min_range < target:
                center = (vertices_np[:, flat_axis].max() + vertices_np[:, flat_axis].min()) / 2
                scale_factor = target / max(min_range, 1e-6)
                vertices_np[:, flat_axis] = center + (vertices_np[:, flat_axis] - center) * scale_factor
                print(f"[shape inflate] axis={flat_axis}, {min_range:.4f} → {target:.4f}")

        print(f"[완료] 버텍스: {len(vertices_np)}, 페이스: {len(faces_np)}")
        vertices_np, faces_np, colors_np = _decimate_mesh(vertices_np, faces_np, colors_np)

        body = orjson.dumps({
            "success": True,
            "type": "vertex_color",
            "mesh": {
                "vertices": vertices_np.tolist(),
                "faces":    faces_np.tolist(),
                "colors":   colors_np.tolist(),
            },
        })
        return Response(content=body, media_type="application/json")

    except Exception as e:
        import traceback
        traceback.print_exc()
        if CACHE_SAM3D_PIPELINE:
            request.app.state.sam3d_pipeline = None
        raise HTTPException(500, f"메쉬 생성 실패: {e}")
    finally:
        if not CACHE_SAM3D_PIPELINE:
            del pipeline
        gc.collect()
        torch.cuda.empty_cache()
        print("SAM3D GPU 캐시 정리 완료")
