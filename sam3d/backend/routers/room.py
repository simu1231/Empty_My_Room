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


def _flatten_room_mesh(vertices_np: np.ndarray, snap_strength: float = 0.9, snap_zone: float = 0.28) -> np.ndarray:
    """SAM3D 메쉬의 울퉁불퉁한 벽/바닥을 평면으로 snap하여 교정"""
    v = vertices_np.copy().astype(np.float32)
    x_min, y_min, z_min = v.min(axis=0)
    x_max, y_max, z_max = v.max(axis=0)
    dx = max(x_max - x_min, 1e-6)
    dy = max(y_max - y_min, 1e-6)
    dz = max(z_max - z_min, 1e-6)

    # 바닥·뒷벽·좌벽·우벽 4면까지 정규화 거리 (천장·앞벽 제외)
    d_floor = (v[:, 1] - y_min) / dy
    d_back  = (v[:, 2] - z_min) / dz
    d_left  = (v[:, 0] - x_min) / dx
    d_right = (x_max - v[:, 0]) / dx

    all_d   = np.stack([d_floor, d_back, d_left, d_right], axis=1)
    nearest = np.argmin(all_d, axis=1)
    min_d   = all_d[np.arange(len(v)), nearest]

    # snap_zone 안에 있는 vertex만 해당 면 쪽으로 당김
    alpha = np.clip(snap_strength * (1.0 - min_d / snap_zone), 0.0, 1.0)
    alpha[min_d >= snap_zone] = 0.0

    v[nearest == 0, 1] += (y_min - v[nearest == 0, 1]) * alpha[nearest == 0]  # 바닥
    v[nearest == 1, 2] += (z_min - v[nearest == 1, 2]) * alpha[nearest == 1]  # 뒷벽
    v[nearest == 2, 0] += (x_min - v[nearest == 2, 0]) * alpha[nearest == 2]  # 왼쪽
    v[nearest == 3, 0] += (x_max - v[nearest == 3, 0]) * alpha[nearest == 3]  # 오른쪽
    return v


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

    # SAM2 마스크 → 벽/바닥 색 추출
    orig_mask = sam2.predict(img_np, room_pts)["mask"].astype(bool)
    h_img, w_img = img_np.shape[:2]

    # 바닥: 이미지 하단 15% 영역 (가장 확실한 바닥 픽셀)
    floor_row_start = int(h_img * 0.85)
    floor_strip = img_np[floor_row_start:, int(w_img*0.1):int(w_img*0.9)]
    floor_color = (floor_strip.mean(axis=(0, 1)) / 255.0).tolist()

    # 벽: SAM2 마스크 상단 50% 영역 평균
    rows = np.where(orig_mask.any(axis=1))[0]
    if len(rows) >= 2:
        y_mid = int(rows[0] + (rows[-1] - rows[0]) * 0.50)
        wall_region = orig_mask.copy(); wall_region[y_mid:, :] = False
    else:
        wall_region = orig_mask
    wall_color = (img_np[wall_region].mean(axis=0) / 255.0).tolist() if wall_region.any() else [0.88, 0.88, 0.88]
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
            with_layout_postprocess=True,
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

        # SAM3D vertex color를 힌트로 벽/바닥 분류 후 이미지 실제 색 입히기
        if glb is not None and hasattr(glb.visual, 'vertex_colors') and glb.visual.vertex_colors is not None:
            sam3d_colors = np.array(glb.visual.vertex_colors[:, :3], dtype=np.float32) / 255.0
        elif raw_mesh is not None and hasattr(raw_mesh, 'vertex_attrs') and raw_mesh.vertex_attrs is not None:
            sam3d_colors = np.clip(raw_mesh.vertex_attrs[:, :3].cpu().float().numpy() * 1.1, 0, 1)
        else:
            sam3d_colors = np.full((len(vertices_np), 3), 0.5, dtype=np.float32)

        fc = np.array(floor_color, dtype=np.float32)

        # SAM3D가 이미지에서 직접 투영한 색 그대로 사용 (벽마다 다른 색 보존)
        colors_np = sam3d_colors.astype(np.float32)

        # Y값으로 바닥만 따로 보정 (PyTorch3D에서 y.min()이 바닥)
        y = vertices_np[:, 1]
        is_floor = y <= (y.min() + 0.20 * (y.max() - y.min()))
        colors_np[is_floor] = fc
        print(f"[색상] SAM3D 투영 색 사용, 바닥만 보정")

        # 후처리: 구멍 메우기
        try:
            import trimesh, trimesh.repair

            mesh_tri = trimesh.Trimesh(
                vertices=vertices_np,
                faces=faces_np,
                vertex_colors=(colors_np * 255).astype(np.uint8),
                process=False,
            )

            # 1) degenerate face 제거 (면적 거의 0인 삼각형)
            face_mask = mesh_tri.area_faces > 1e-8
            mesh_tri.update_faces(face_mask)

            # 2) 구멍 메우기
            trimesh.repair.fill_holes(mesh_tri)

            # 3) 약한 Laplacian smoothing 1회 (이음새 거친 부분 완화)
            trimesh.smoothing.filter_laplacian(mesh_tri, iterations=1, lamb=0.2)

            vertices_np = np.array(mesh_tri.vertices, dtype=np.float32)
            faces_np    = np.array(mesh_tri.faces, dtype=np.int32)

            # 벽/바닥 평면 snap으로 울퉁불퉁함 제거
            vertices_np = _flatten_room_mesh(vertices_np)

            # 천장·앞벽 face 제거
            y_min_v = vertices_np[:, 1].min(); y_max_v = vertices_np[:, 1].max()
            z_min_v = vertices_np[:, 2].min(); z_max_v = vertices_np[:, 2].max()
            dy_v = y_max_v - y_min_v
            dz_v = z_max_v - z_min_v
            face_centers = vertices_np[faces_np].mean(axis=1)
            is_ceiling = face_centers[:, 1] >= (y_max_v - 0.03 * dy_v)
            is_front   = face_centers[:, 2] >= (z_max_v - 0.05 * dz_v)
            keep = ~(is_ceiling | is_front)
            faces_np = faces_np[keep]
            print(f"[천장/앞벽 제거] {(~keep).sum()}개 face 제거")

            # 색 재할당: hole fill로 추가된 vertex 처리
            new_n = len(vertices_np)
            old_n = len(colors_np)
            if new_n > old_n:
                extra_colors = np.tile(fc, (new_n - old_n, 1))
                colors_np = np.vstack([colors_np, extra_colors])
            else:
                colors_np = colors_np[:new_n]
            # 바닥 보정 (PyTorch3D에서 y.min()이 바닥)
            y2 = vertices_np[:, 1]
            is_floor2 = y2 <= (y2.min() + 0.06 * (y2.max() - y2.min()))
            colors_np[is_floor2] = fc

            print(f"[후처리 완료] 버텍스: {len(vertices_np)}, 페이스: {len(faces_np)}")
        except Exception as pe:
            print(f"[후처리 스킵] {pe}")
            print(f"[완료] 버텍스: {len(vertices_np)}, 페이스: {len(faces_np)}")

        # 바닥 커버리지 체크 → 부족하면 바닥 평면 추가
        y_arr   = vertices_np[:, 1]
        y_min_v = y_arr.min(); y_max_v = y_arr.max()
        floor_v = vertices_np[y_arr <= (y_min_v + 0.15 * (y_max_v - y_min_v))]
        if len(floor_v) > 3:
            fx = floor_v[:, 0].max() - floor_v[:, 0].min()
            fz = floor_v[:, 2].max() - floor_v[:, 2].min()
            bx = vertices_np[:, 0].max() - vertices_np[:, 0].min()
            bz = vertices_np[:, 2].max() - vertices_np[:, 2].min()
            coverage = (fx * fz) / (bx * bz + 1e-6)
        else:
            coverage = 0.0

        print(f"[바닥 커버리지] {coverage:.2f}")
        if coverage < 0.4:
            print(f"[바닥 추가] 커버리지 {coverage:.2f} → 바닥 평면 삽입")
            x0, x1 = vertices_np[:, 0].min(), vertices_np[:, 0].max()
            z0, z1 = vertices_np[:, 2].min(), vertices_np[:, 2].max()
            yf = y_min_v
            n  = len(vertices_np)
            new_v = np.array([[x0,yf,z0],[x1,yf,z0],[x1,yf,z1],[x0,yf,z1]], dtype=np.float32)
            new_f = np.array([[n,n+1,n+2],[n,n+2,n+3],[n+2,n+1,n],[n+3,n+2,n]], dtype=np.int32)
            new_c = np.tile(fc, (4, 1))
            vertices_np = np.vstack([vertices_np, new_v])
            faces_np    = np.vstack([faces_np,    new_f])
            colors_np   = np.vstack([colors_np,   new_c])

        body = orjson.dumps({
            "success":     True,
            "wall_color":  wall_color,
            "floor_color": floor_color,
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
