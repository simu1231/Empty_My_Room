import io
import gc
import json
import base64
import numpy as np
import torch
import orjson
from PIL import Image
from fastapi import APIRouter, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse, Response

router = APIRouter()

PIPELINE_CONFIG = '/home/tmvlem5671/sam-3d-objects/checkpoints/hf/checkpoints/pipeline.yaml'
WORKSPACE_DIR   = '/home/tmvlem5671/sam-3d-objects/checkpoints/hf/checkpoints'
MIN_INPUT_SIZE  = 512
MAX_INPUT_SIZE  = 1024


def _crop_and_resize(rgb: np.ndarray, mask: np.ndarray):
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if not rows.any() or not cols.any():
        return rgb, mask
    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]
    pad = 8
    rmin = max(0, rmin - pad); rmax = min(rgb.shape[0], rmax + pad + 1)
    cmin = max(0, cmin - pad); cmax = min(rgb.shape[1], cmax + pad + 1)
    rgb = rgb[rmin:rmax, cmin:cmax]
    mask = mask[rmin:rmax, cmin:cmax]
    h, w = rgb.shape[:2]
    cur_max = max(h, w)
    if cur_max < MIN_INPUT_SIZE:
        scale = MIN_INPUT_SIZE / cur_max
    elif cur_max > MAX_INPUT_SIZE:
        scale = MAX_INPUT_SIZE / cur_max
    else:
        scale = 1.0
    if scale != 1.0:
        new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
        rgb = np.array(Image.fromarray(rgb).resize((new_w, new_h), Image.LANCZOS))
        mask = np.array(Image.fromarray(mask.astype(np.uint8) * 255).resize((new_w, new_h), Image.NEAREST)) > 127
    print(f"[전처리] 크롭+리사이즈 완료: {rgb.shape[1]}x{rgb.shape[0]}")
    return rgb, mask


@router.post("/extract-colors")
async def extract_room_colors(image: UploadFile = File(...)):
    img_bytes = await image.read()
    image_pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img_np = np.array(image_pil)

    h, w = img_np.shape[:2]

    floor_region = image_pil.crop((int(w*0.1), int(h*0.75), int(w*0.9), h))
    floor_np = np.array(floor_region)
    floor_color = (floor_np.mean(axis=(0,1)) / 255.0).tolist()

    wall_region = image_pil.crop((int(w*0.2), int(h*0.1), int(w*0.8), int(h*0.6)))
    wall_np = np.array(wall_region)
    wall_color = (wall_np.mean(axis=(0,1)) / 255.0).tolist()

    floor_tex = floor_region.resize((512, 512), Image.LANCZOS)
    floor_buf = io.BytesIO()
    floor_tex.save(floor_buf, format='JPEG', quality=85)
    floor_tex_b64 = base64.b64encode(floor_buf.getvalue()).decode()

    wall_tex = wall_region.resize((512, 512), Image.LANCZOS)
    wall_buf = io.BytesIO()
    wall_tex.save(wall_buf, format='JPEG', quality=85)
    wall_tex_b64 = base64.b64encode(wall_buf.getvalue()).decode()

    return JSONResponse({
        "success": True,
        "floor_color": floor_color,
        "wall_color": wall_color,
        "floor_texture": floor_tex_b64,
        "wall_texture": wall_tex_b64,
    })


@router.post("/generate3d")
async def generate_room_3d(
    request: Request,
    image: UploadFile = File(...),
    room_points: str = Form(...),
):
    img_bytes = await image.read()
    img_pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img_np = np.array(img_pil)

    room_pts = json.loads(room_points)
    if not room_pts:
        return JSONResponse({"success": False, "error": "포인트가 없습니다"}, status_code=400)

    sam2 = request.app.state.sam2
    if sam2 is None:
        return JSONResponse({"success": False, "error": "SAM2 not loaded"}, status_code=500)

    # SAM2 마스크 → 이미지 Y 분할로 벽/바닥 평균색 추출
    orig_mask = sam2.predict(img_np, room_pts)["mask"].astype(bool)
    rows = np.where(orig_mask.any(axis=1))[0]
    if len(rows) >= 2:
        y_mid = int(rows[0] + (rows[-1] - rows[0]) * 0.65)
        wall_region  = orig_mask.copy(); wall_region[y_mid:, :]  = False
        floor_region = orig_mask.copy(); floor_region[:y_mid, :] = False
    else:
        wall_region = floor_region = orig_mask
    wall_color  = (img_np[wall_region].mean(axis=0)  / 255.0).tolist() if wall_region.any()  else [0.88, 0.88, 0.88]
    floor_color = (img_np[floor_region].mean(axis=0) / 255.0).tolist() if floor_region.any() else [0.65, 0.55, 0.40]
    print(f"[색상] 벽: {[round(c,3) for c in wall_color]}, 바닥: {[round(c,3) for c in floor_color]}")

    # SAM3D: mesh 생성
    rgb, mask = _crop_and_resize(img_np.copy(), orig_mask)
    rgb[~mask] = 128
    mask_uint8 = mask.astype(np.uint8) * 255

    pipeline = getattr(request.app.state, 'sam3d_pipeline', None)
    if pipeline is None:
        print("SAM3D 파이프라인 로드 중...")
        from omegaconf import OmegaConf
        from hydra.utils import instantiate
        config = OmegaConf.load(PIPELINE_CONFIG)
        config.rendering_engine   = 'pytorch3d'
        config.compile_model      = False
        config.workspace_dir      = WORKSPACE_DIR
        config.ss_inference_steps   = 25
        config.slat_inference_steps = 25
        config.slat_cfg_strength    = 1
        pipeline = instantiate(config)
        request.app.state.sam3d_pipeline = pipeline
        print("SAM3D 파이프라인 로드 완료!")

    try:
        result = pipeline.run(
            rgb, mask_uint8, seed=42,
            stage1_only=False,
            with_mesh_postprocess=True,
            with_texture_baking=False,
            with_layout_postprocess=False,
            use_vertex_color=True,
            pointmap=None,
        )

        glb      = result.get('glb')
        raw_mesh = result.get('mesh')

        if glb is not None:
            vertices_np = np.array(glb.vertices, dtype=np.float32)
            faces_np    = np.array(glb.faces,    dtype=np.int32)
        elif raw_mesh is not None:
            if isinstance(raw_mesh, list): raw_mesh = raw_mesh[0]
            vertices_np = raw_mesh.vertices.cpu().float().numpy()
            faces_np    = raw_mesh.faces.cpu().numpy().astype(np.int32)
        else:
            return JSONResponse({"success": False, "error": "메쉬 생성 실패"}, status_code=500)

        # SAM3D vertex color 대신 실제 이미지 색을 vertex Y값 기준으로 입히기
        # 백엔드 Y+가 높을수록 = 프론트 Y-flip 후 아래쪽(바닥)
        y = vertices_np[:, 1]
        floor_thresh = y.max() - 0.12 * (y.max() - y.min())  # 상위 12%만 바닥
        is_floor = y >= floor_thresh
        colors_np = np.where(
            is_floor[:, None],
            np.array(floor_color, dtype=np.float32)[None, :],
            np.array(wall_color,  dtype=np.float32)[None, :],
        ).astype(np.float32)

        print(f"[완료] 버텍스: {len(vertices_np)}, 페이스: {len(faces_np)}")

        body = orjson.dumps({
            "success": True,
            "mesh": {
                "vertices": vertices_np.tolist(),
                "faces":    faces_np.tolist(),
                "colors":   colors_np.tolist(),
            }
        })
        return Response(content=body, media_type="application/json")

    except Exception as e:
        import traceback; traceback.print_exc()
        request.app.state.sam3d_pipeline = None  # 오류 시 캐시 초기화
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
    finally:
        gc.collect()
        torch.cuda.empty_cache()
