import io
import gc
import json
import base64
import cv2
import numpy as np
import requests
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
ULAYOUT_SIDECAR_URL = 'http://localhost:8002/infer'
ULAYOUT_RECTIFY_SIDECAR_URL = 'http://localhost:8002/rectify'

# 패치 타일 폴백에서 "실제 무늬/질감" vs "매끈한 벽 + 미세한 그림자/조명 그라데이션"을
# 가르는 std 임계값. 그라데이션만 있는 패치를 타일링하면 반복 이음새가 줄무늬처럼 튀어서
# 오히려 단색보다 부자연스러움 -> 이 미만이면 타일링 대신 평균색 단색으로 채움.
#
# *** NEEDS TUNING WITH REAL DATA ***
# raw std는 "저주파 조명 그라데이션"과 "고주파 실제 재질 무늬"를 구분 못해서, 글레어처럼
# 밝기만 확 튀는 영역도 std가 높게 나와 잘못 "패턴 있음"으로 판정될 수 있음. 그래서 이제는
# delight() 적용 후(조명 성분 제거된) 이미지의 std, 즉 RESIDUAL std로 판정한다. raw std는
# 비교용으로만 같이 로그에 남긴다.
# 실측 기준(이번 세션, wall 기준): 꽃무늬 벽지 raw_std≈26.65(패턴 O) vs 그라데이션 있는
# 무지 벽 raw_std≈13.57(패턴 X, 문제 케이스) — 그 사이로 잡은 값이 18.0. resid_std 기준
# 임계값은 아직 실측 데이터가 부족해 일단 같은 18.0으로 시작.
PATCH_FLAT_STD_THRESHOLD = 18.0  # raw_std 로깅/구 버전 호환용
PATCH_RESID_STD_THRESHOLD = 18.0  # resid_std 기준 실제 판정에 쓰는 값 — *** NEEDS TUNING ***

# *** NEEDS TUNING WITH REAL DATA ***
# 처음엔 room_rectify.py처럼 "영역 크기의 8%" 같은 상대 비율로 블러 커널을 잡았는데,
# tier-2 후보 영역(사진마다 해상도가 제각각) 기준으로는 꽃무늬 벽지 패치의 resid_std가
# 17.3까지 떨어져(원래 raw_std 26.65였던 걸 임계값 18 밑으로 잘못 깎아버림 — delight가
# 벽지 무늬 자체까지 "조명"으로 오인해 지워버리는 회귀 발견) 절대 픽셀 크기로 바꿈.
# 100x100 패치 기준 실측: k=9→꽃무늬 resid_std 16.98(잘못 flatten), k=21→20.96(정상 유지),
# 그라데이션 벽은 k값 상관없이 12.7 근처로 안정적 — k=21로 설정.
DELIGHT_KERNEL_PX = 21
SATURATION_THRESHOLD = 245  # 이 값 이상인 채널이 하나라도 있으면 "글레어로 클리핑된 픽셀"로 보고
                             # 패치 후보에서 제외 (delight로도 복원 불가능한 정보 손실 영역이므로)


def _delight(region_bgr: np.ndarray) -> np.ndarray:
    """저주파 조명(그림자·글레어)을 나눗셈(flat-fielding)으로 제거, 고주파 재질만 남김."""
    k = DELIGHT_KERNEL_PX
    if k % 2 == 0:
        k += 1
    img_f = region_bgr.astype(np.float32) + 1.0
    illum = cv2.GaussianBlur(img_f, (k, k), 0)
    ratio = img_f / illum
    mean_val = img_f.reshape(-1, 3).mean(axis=0)
    return np.clip(ratio * mean_val, 0, 255).astype(np.uint8)


def _saturation_mask(region_bgr: np.ndarray, threshold: float = SATURATION_THRESHOLD) -> np.ndarray:
    return (region_bgr >= threshold).any(axis=2)


# delight()는 저주파 그라데이션만 없앨 뿐 크롭 전체가 이미 밝았다면(글레어가 평균을 끌어올림)
# 결과물 밝기가 여전히 높게 유지됨 -> Three.js 씬 조명(ambient+directional 여러 개 합산) 아래서
# 실제로 흰색에 가깝게 날아가는 걸 렌더링에서 확인 -> 대비 스트레치 추가.
CONTRAST_LOW_PCT, CONTRAST_HIGH_PCT = 2, 98
CONTRAST_TARGET_LOW, CONTRAST_TARGET_HIGH = 70, 225  # *** NEEDS TUNING WITH REAL DATA ***


def _contrast_stretch(patch_bgr: np.ndarray) -> np.ndarray:
    """채널별 percentile 기반 대비 스트레치 — 렌더러 조명 아래서도 재질 알갱이가 보이게."""
    img_f = patch_bgr.astype(np.float32)
    out = np.zeros_like(img_f)
    for c in range(3):
        ch = img_f[:, :, c]
        lo, hi = np.percentile(ch, [CONTRAST_LOW_PCT, CONTRAST_HIGH_PCT])
        scale = (CONTRAST_TARGET_HIGH - CONTRAST_TARGET_LOW) / max(hi - lo, 1.0)
        out[:, :, c] = (ch - lo) * scale + CONTRAST_TARGET_LOW
    return np.clip(out, 0, 255).astype(np.uint8)


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



def _least_busy_patch(region_np: np.ndarray, exclude_mask: np.ndarray = None,
                       patch_size: int = 100, stride: int = 50) -> dict:
    """
    region_np 안에서 "가장 무난한"(타일링해도 이음새/글레어가 안 튀는) patch_size x patch_size
    패치를 찾는다 (RANSAC 코너 검출 실패 시의 폴백 텍스처 재료).

    exclude_mask: region_np와 같은 (h, w) shape의 bool 배열. True인 곳(가구/창문/커튼 등
    SAM2 마스크로 지운 영역)을 절반 넘게 포함하는 패치는 후보에서 제외한다. 여기에 더해
    채도(밝기) 클리핑된 글레어 영역도 자동으로 같이 제외한다 — delight()로도 복원 못 하는
    정보 손실 영역이 "제일 무난한 곳"으로 잘못 뽑히는 걸 막기 위함.

    선택 기준은 raw std가 아니라 delight() 적용 후의 RESIDUAL std(고주파 재질 성분만
    남긴 것의 분산) — 저주파 조명 그라데이션과 실제 재질 무늬를 구분하기 위함.

    반환: {"delit": 조명 제거된 패치(타일링용 최종 결과), "raw": 원본 패치(로그 비교용),
           "raw_std": float, "resid_std": float}
    """
    h, w = region_np.shape[:2]
    if h < patch_size or w < patch_size:
        scale = patch_size / max(min(h, w), 1)
        new_w, new_h = max(patch_size, int(w * scale)), max(patch_size, int(h * scale))
        region_np = np.array(Image.fromarray(region_np).resize((new_w, new_h), Image.LANCZOS))
        if exclude_mask is not None:
            exclude_mask = np.array(
                Image.fromarray(exclude_mask.astype(np.uint8) * 255).resize((new_w, new_h), Image.NEAREST)
            ) > 127
        h, w = region_np.shape[:2]

    sat_mask = _saturation_mask(region_np)
    combined_exclude = sat_mask if exclude_mask is None else (exclude_mask | sat_mask)
    region_delit = _delight(region_np)

    def _scan(mask):
        best = None
        for y in range(0, h - patch_size + 1, stride):
            for x in range(0, w - patch_size + 1, stride):
                if mask is not None and mask[y:y + patch_size, x:x + patch_size].mean() > 0.5:
                    continue
                delit_patch = region_delit[y:y + patch_size, x:x + patch_size]
                resid_std = float(delit_patch.astype(np.float32).std())
                if best is None or resid_std < best[0]:
                    raw_patch = region_np[y:y + patch_size, x:x + patch_size]
                    raw_std = float(raw_patch.astype(np.float32).std())
                    best = (resid_std, delit_patch, raw_patch, raw_std)
        return best

    best = _scan(combined_exclude)
    if best is None:  # 후보 전체가 마스크(가구+글레어)로 덮인 극단적 경우 -> 마스크 무시하고 재탐색
        best = _scan(None)
    if best is None:  # patch_size보다 region이 작았던 극단적 경우
        raw = region_np[:patch_size, :patch_size]
        delit = region_delit[:patch_size, :patch_size]
        std = float(raw.astype(np.float32).std())
        return {"delit": delit, "raw": raw, "raw_std": std, "resid_std": std}

    resid_std, delit_patch, raw_patch, raw_std = best
    return {"delit": delit_patch, "raw": raw_patch, "raw_std": raw_std, "resid_std": resid_std}


def _flatten_if_smooth(patch_np: np.ndarray, std: float, threshold: float = PATCH_RESID_STD_THRESHOLD):
    """
    resid_std가 실제 무늬/질감이라 보기엔 너무 낮으면(그림자·조명 그라데이션만 있던 경우 —
    delight 이후에도 특징이 거의 안 남는 경우) 타일링하지 않고 평균색으로 단색 채움.
    반환: (patch_np 또는 단색으로 채운 동일 shape 배열, flattened 여부)
    """
    std = float(patch_np.astype(np.float32).std())
    if std < threshold:
        mean_color = patch_np.reshape(-1, 3).mean(axis=0)
        flat = np.full_like(patch_np, mean_color)
        return flat, True
    return patch_np, False


@router.post("/extract-colors")
async def extract_room_colors(image: UploadFile = File(...), pitch_deg: float = Form(0.0),
                               mask: UploadFile = File(None)):
    img_bytes = await image.read()
    image_pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img_np = np.array(image_pil)

    h, w = img_np.shape[:2]

    # SAM2 가구/창문/커튼 마스크(인페인팅에 쓴 그 마스크) — 있으면 패치 후보에서 제외.
    # 블라인드·침대 그림자처럼 분산은 낮지만 벽/바닥이 아닌 영역이 골라지는 걸 막기 위함.
    exclude_mask = None
    if mask is not None:
        mask_bytes = await mask.read()
        if mask_bytes:
            mask_pil = Image.open(io.BytesIO(mask_bytes)).convert("L")
            mask_np = np.array(mask_pil)
            if mask_np.shape != (h, w):
                mask_np = np.array(mask_pil.resize((w, h), Image.NEAREST))
            exclude_mask = mask_np > 127

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

    # ── 타일링용 "무늬 균일" 패치 (RANSAC 실패해도 쓸 수 있는 기본 폴백 텍스처) ──
    # 세로 1/3 분할이 기본. pitch_deg(카메라가 아래를 보는 정도)로 경계를 살짝 보정:
    # 아래를 더 많이 볼수록(pitch 음수 쪽 클수록) 벽은 더 위쪽에서 끝나고 바닥은 더 일찍 시작.
    wall_frac = float(np.clip(0.34 - pitch_deg / 300.0, 0.20, 0.45))
    floor_frac = float(np.clip(0.34 + pitch_deg / 300.0, 0.20, 0.45))

    # 임시방편(마스크 없을 때 보조 안전장치): 맨 위/맨 아래/가장자리는 커튼봉·창틀·
    # 걸레받이 등이 자주 걸리니 후보 영역을 가운데 쪽으로 살짝 좁힘. 근본 대책은 exclude_mask.
    wall_row0, wall_row1 = int(h * 0.06), max(int(h * wall_frac), int(h * 0.06) + 1)
    floor_row0, floor_row1 = min(int(h * (1 - floor_frac)), h - 2), int(h * 0.96)
    col0, col1 = int(w * 0.12), int(w * 0.88)

    wall_cand = img_np[wall_row0:wall_row1, col0:col1]
    floor_cand = img_np[floor_row0:floor_row1, col0:col1]
    wall_mask_cand = exclude_mask[wall_row0:wall_row1, col0:col1] if exclude_mask is not None else None
    floor_mask_cand = exclude_mask[floor_row0:floor_row1, col0:col1] if exclude_mask is not None else None

    wall_pick = _least_busy_patch(wall_cand, wall_mask_cand)
    floor_pick = _least_busy_patch(floor_cand, floor_mask_cand)

    wall_patch_np, wall_flattened = _flatten_if_smooth(wall_pick["delit"], wall_pick["resid_std"])
    floor_patch_np, floor_flattened = _flatten_if_smooth(floor_pick["delit"], floor_pick["resid_std"])
    if not wall_flattened:
        wall_patch_np = _contrast_stretch(wall_patch_np)
    if not floor_flattened:
        floor_patch_np = _contrast_stretch(floor_patch_np)
    print(f"[patch] wall  raw_std={wall_pick['raw_std']:.2f} resid_std={wall_pick['resid_std']:.2f} "
          f"flattened={wall_flattened}  |  floor raw_std={floor_pick['raw_std']:.2f} "
          f"resid_std={floor_pick['resid_std']:.2f} flattened={floor_flattened}  "
          f"(threshold={PATCH_RESID_STD_THRESHOLD})")
    wall_patch_std, floor_patch_std = wall_pick["resid_std"], floor_pick["resid_std"]

    def _patch_to_b64(patch_np, tile_size=256):
        img = Image.fromarray(patch_np).resize((tile_size, tile_size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=88)
        return base64.b64encode(buf.getvalue()).decode()

    wall_patch_b64 = _patch_to_b64(wall_patch_np)
    floor_patch_b64 = _patch_to_b64(floor_patch_np)

    return JSONResponse({
        "success": True,
        "floor_color": floor_color,
        "wall_color": wall_color,
        "floor_texture": floor_tex_b64,
        "wall_texture": wall_tex_b64,
        "floor_patch": floor_patch_b64,
        "wall_patch": wall_patch_b64,
        "wall_patch_std": wall_patch_std,           # resid_std (실제 판정에 쓰인 값)
        "floor_patch_std": floor_patch_std,
        "wall_patch_raw_std": wall_pick["raw_std"],   # 비교/튜닝용
        "floor_patch_raw_std": floor_pick["raw_std"],
        "wall_patch_flattened": wall_flattened,
        "floor_patch_flattened": floor_flattened,
    })


@router.post("/layout")
async def estimate_room_layout(image: UploadFile = File(...), camera_height_m: float = Form(1.6)):
    """
    uLayout 사이드카(별도 conda env, localhost:8002)로 빈방 이미지를 보내
    방 폭/깊이/높이(m)를 추정해서 돌려준다. 사이드카가 꺼져있으면 프론트가
    수동 입력값으로 폴백할 수 있도록 success:false로 응답한다.
    """
    img_bytes = await image.read()
    try:
        resp = requests.post(
            ULAYOUT_SIDECAR_URL,
            files={"image": (image.filename or "room.jpg", img_bytes, image.content_type or "image/jpeg")},
            data={"camera_height_m": camera_height_m},
            timeout=30,
        )
        resp.raise_for_status()
        return JSONResponse(resp.json())
    except requests.exceptions.RequestException as e:
        return JSONResponse(
            {"success": False, "error": f"uLayout 서버에 연결할 수 없습니다: {e}"},
            status_code=503,
        )


@router.post("/rectify_textures")
async def rectify_room_textures(image: UploadFile = File(...), camera_height_m: float = Form(1.6)):
    """
    uLayout 사이드카의 /rectify를 그대로 프록시. 벽/바닥/천장 5개 평면을 실사 텍스처로
    rectify한 결과(base64 JPEG)를 돌려준다. 사이드카가 꺼져있거나 코너 검출/solvePnP가
    실패하면 success:false로 응답 (프론트는 procedural box의 기존 단색 방식으로 폴백).
    """
    img_bytes = await image.read()
    try:
        resp = requests.post(
            ULAYOUT_RECTIFY_SIDECAR_URL,
            files={"image": (image.filename or "room.jpg", img_bytes, image.content_type or "image/jpeg")},
            data={"camera_height_m": camera_height_m},
            timeout=60,
        )
        resp.raise_for_status()
        return JSONResponse(resp.json())
    except requests.exceptions.RequestException as e:
        return JSONResponse(
            {"success": False, "error": f"uLayout 서버에 연결할 수 없습니다: {e}"},
            status_code=503,
        )


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
