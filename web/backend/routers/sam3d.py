import io
import numpy as np
import cv2
from PIL import Image
from fastapi import APIRouter, UploadFile, File, Request, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

@router.post("/pointcloud")
async def generate_pointcloud(
    request: Request,
    image: UploadFile = File(...),
):
    sam3d = request.app.state.sam3d

    if sam3d is None:
        raise HTTPException(503, "SAM3D 모델이 로드되지 않았습니다")

    img_bytes = await image.read()
    image_pil = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    image_np  = np.array(image_pil)

    # 알파 채널을 마스크로 사용
    alpha = image_np[..., 3]
    rgb   = image_np[..., :3]

    # 배경을 흰색으로 채운 RGB 이미지
    bg = np.ones_like(rgb) * 255
    mask_3c = (alpha > 127)[..., np.newaxis]
    rgb_filled = np.where(mask_3c, rgb, bg).astype(np.uint8)

    # 리사이즈
    MAX_SIZE = 512
    h, w = rgb_filled.shape[:2]
    if max(h, w) > MAX_SIZE:
        scale = MAX_SIZE / max(h, w)
        rgb_filled = cv2.resize(rgb_filled, (int(w*scale), int(h*scale)))
        alpha = cv2.resize(alpha, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_NEAREST)

    # 디버그: 이미지 저장
    from PIL import Image as PILImage
    PILImage.fromarray(rgb_filled).save('/home/tmvlem5671/debug_furniture.png')
    PILImage.fromarray(alpha).save('/home/tmvlem5671/debug_alpha.png')
    print(f"이미지 크기: {rgb_filled.shape}, 알파 픽셀 수: {(alpha > 127).sum()}")
    print("Depth Anything 깊이 추정 중...")
    depth_np = sam3d.get_depth(rgb_filled)
    print("깊이 추정 완료!")

    print("가구 포인트 클라우드 생성 중...")
    h, w = depth_np.shape
    cx, cy = w / 2, h / 2
    fx = fy = max(h, w) * 1.2  # 초점 거리 (픽셀 단위), 경험적으로 1.2배 정도가 적당

    points = []
    colors = []

    for v in range(0, h, 1):
        for u in range(0, w, 1):
            # 알파 마스크로 가구 부분만
            if alpha[v, u] < 127:
                continue
            z = float(depth_np[v, u])
            if z <= 0:
                continue
            x = (u - cx) * z / fx
            y = (v - cy) * z / fy
            points.append([x, y, z])
            colors.append(rgb_filled[v, u] / 255.0)

    print(f"포인트 클라우드 생성 완료! {len(points)}개 포인트")

    if len(points) == 0:
        raise HTTPException(400, "포인트 클라우드 생성 실패")

    points = np.array(points, dtype=np.float32)
    colors = np.array(colors, dtype=np.float32)

    return JSONResponse({
        "success": True,
        "pointcloud": {
            "points": points.tolist(),
            "colors": colors.tolist(),
        },
        "point_count": int(len(points)),
    })