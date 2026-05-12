import io
import gc
import torch
import numpy as np
import orjson
from PIL import Image
from fastapi import APIRouter, UploadFile, File, Request, HTTPException
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
        config.slat_cfg_strength    = 5   # YAML이 1로 낮춰놓은 것을 기본값(5)으로 복원
        pipeline = instantiate(config)
        if CACHE_SAM3D_PIPELINE:
            request.app.state.sam3d_pipeline = pipeline
            print("SAM3D 파이프라인 캐시 완료!")

    try:
        result = pipeline.run(
            rgb, mask, seed=123,
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
