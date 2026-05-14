import io
import gc
import time
import json
import torch
import numpy as np
import orjson
from typing import Optional
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
    full_image: Optional[UploadFile] = File(None),
    bbox: str = Form(''),
):
    img_bytes = await image.read()
    image_pil = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    image_np  = np.array(image_pil)
    alpha     = image_np[..., 3]
    rgb       = image_np[..., :3]
    mask      = alpha > 127

    is_thin_flat_cat = any(c in category for c in FLAT_CATEGORIES | THIN_CATEGORIES)

    # thin/flat 카테고리: 전체 방 이미지에서 컨텍스트 패딩 포함한 넓은 크롭 생성
    full_scene_pointmap = None
    if is_thin_flat_cat and full_image and bbox:
        try:
            full_bytes = await full_image.read()
            full_pil   = Image.open(io.BytesIO(full_bytes)).convert('RGB')
            full_np    = np.array(full_pil)
            fh, fw     = full_np.shape[:2]

            MAX_EXTRACT = 1024
            fscale = 1.0
            if max(fh, fw) > MAX_EXTRACT:
                fscale = MAX_EXTRACT / max(fh, fw)
                full_np = np.array(Image.fromarray(full_np).resize(
                    (int(fw * fscale), int(fh * fscale)), Image.LANCZOS))
                fh, fw = full_np.shape[:2]

            bbox_list = json.loads(bbox)
            x1, y1, x2, y2 = [int(c) for c in bbox_list]

            # 가구 주변 15% 패딩 추가 → MoGe context 제공, 배경 shape 영향 최소화
            bw, bh = x2 - x1, y2 - y1
            pad_x, pad_y = int(bw * 0.15), int(bh * 0.15)
            cx1 = max(0, x1 - pad_x)
            cy1 = max(0, y1 - pad_y)
            cx2 = min(fw, x2 + pad_x)
            cy2 = min(fh, y2 + pad_y)

            # 전체 방 이미지에서 컨텍스트 포함 크롭
            ctx_rgb  = full_np[cy1:cy2, cx1:cx2]

            # bbox 기준으로 컨텍스트 내 가구 위치 계산
            mx1 = x1 - cx1;  my1 = y1 - cy1
            mx2 = mx1 + (x2 - x1);  my2 = my1 + (y2 - y1)

            # 컨텍스트 크롭 크기에 맞는 마스크 (가구 영역만 True)
            ctx_h, ctx_w = ctx_rgb.shape[:2]
            ctx_mask = np.zeros((ctx_h, ctx_w), dtype=bool)

            # 원본 가구 마스크를 bbox 크기로 리사이즈 후 삽입
            orig_mask_resized = np.array(
                Image.fromarray(mask.astype(np.uint8) * 255).resize(
                    (x2 - x1, y2 - y1), Image.NEAREST)) > 127
            ctx_mask[my1:my2, mx1:mx2] = orig_mask_resized

            # 크기 제한
            if max(ctx_h, ctx_w) > MAX_INPUT_SIZE:
                s = MAX_INPUT_SIZE / max(ctx_h, ctx_w)
                ctx_rgb  = np.array(Image.fromarray(ctx_rgb).resize(
                    (int(ctx_w*s), int(ctx_h*s)), Image.LANCZOS))
                ctx_mask = np.array(Image.fromarray(ctx_mask.astype(np.uint8)*255).resize(
                    (int(ctx_w*s), int(ctx_h*s)), Image.NEAREST)) > 127

            # 배경 그대로 유지 — MoGe가 씬 컨텍스트로 depth 추정해야 함
            rgb  = ctx_rgb
            mask = ctx_mask
            print(f"[컨텍스트 크롭] {rgb.shape[1]}x{rgb.shape[0]}, 가구영역 포함")
            full_scene_pointmap = True
        except Exception as e:
            print(f"[컨텍스트 크롭 실패 → 기존 방식] {e}")
            full_scene_pointmap = None

    if not full_scene_pointmap:
        rgb, mask = _crop_and_resize(rgb, mask)
        rgb_bg = rgb.copy()
        rgb_bg[~mask] = 128
        rgb = rgb_bg
        mask = mask.astype(np.uint8) * 255  # bool → 0/255 (scale calibration 정상화)

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

    # thin/flat 가구: 배경 살린 씬으로 MoGe 먼저 실행(depth), SAM3D엔 gray bg(shape 혼란 방지)
    custom_pointmap = None
    if is_thin_flat_cat and full_scene_pointmap:
        # 1) 외부 MoGe: 실제 씬 컨텍스트로 valid depth 확보
        mask_255 = mask.astype(np.uint8) * 255
        rgba_ctx = np.concatenate([rgb, mask_255[..., None]], axis=-1)
        with torch.no_grad():
            pm_dict = pipeline.compute_pointmap(rgba_ctx)
        custom_pointmap = pm_dict["pointmap"].cpu()  # (3, H, W)
        finite = custom_pointmap.isfinite().all(dim=0).sum().item()
        print(f"[외부 MoGe] shape={tuple(custom_pointmap.shape)}, valid_px={finite}")
        # 2) SAM3D shape 모델엔 gray bg 이미지 전달 (배경 가구에 shape 영향 방지)
        rgb[~mask] = 128
        mask = mask_255  # 0/255 uint8로 교체

    try:
        import random

        _t_sam3d_total = time.time()
        NUM_STAGE1_TRIES = 10

        # Stage 1만 빠르게 여러 번 → 가장 입체적인 seed 선택
        best_seed = None
        best_stage1_score = -1.0
        _t_stage1 = time.time()

        for attempt in range(NUM_STAGE1_TRIES):
            seed = random.randint(0, 2**31)
            r1 = pipeline.run(
                rgb, mask, seed=seed,
                stage1_only=True,
                with_mesh_postprocess=False,
                with_texture_baking=False,
                with_layout_postprocess=False,
                use_vertex_color=True,
                pointmap=custom_pointmap,
            )
            voxel = r1['voxel'].cpu().numpy()
            if any(c in category for c in THIN_CATEGORIES):
                score = float(len(voxel))
            else:
                ranges = voxel.max(axis=0) - voxel.min(axis=0)
                score = float(ranges.min())
            print(f"[Stage1 시도 {attempt+1}/{NUM_STAGE1_TRIES}] seed={seed} score={score:.4f}")
            if score > best_stage1_score:
                best_stage1_score = score
                best_seed = seed

        print(f"[⏱ 처리시간] SAM3D Stage1 탐색 ({NUM_STAGE1_TRIES}회): {time.time()-_t_stage1:.2f}초")
        print(f"[최적 seed 선택] seed={best_seed} score={best_stage1_score:.4f}")

        # 최적 seed로 full 파이프라인 실행
        _t_stage2 = time.time()
        result = pipeline.run(
            rgb, mask, seed=best_seed,
            stage1_only=False,
            with_mesh_postprocess=False,
            with_texture_baking=False,
            with_layout_postprocess=False,
            use_vertex_color=True,
            pointmap=custom_pointmap,
        )
        print(f"[⏱ 처리시간] SAM3D Stage2 (메쉬 생성): {time.time()-_t_stage2:.2f}초")
        print(f"[⏱ 처리시간] SAM3D 전체: {time.time()-_t_sam3d_total:.2f}초")

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
