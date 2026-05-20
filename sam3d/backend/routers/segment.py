import io
import time
import base64
import json
import numpy as np
import cv2
from PIL import Image, ImageOps
from fastapi import APIRouter, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

def image_to_base64(image_np):
    img_pil = Image.fromarray(image_np)
    buf = io.BytesIO()
    img_pil.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

@router.post("/mask")
async def create_mask(
    request: Request,
    image: UploadFile = File(...),
    points: str = Form(...),
):
    _t0 = time.time()
    sam2 = request.app.state.sam2
    if sam2 is None:
        raise HTTPException(503, "SAM2 모델이 로드되지 않았습니다")

    contents = await image.read()
    # ImageOps.exif_transpose: 스마트폰 사진의 EXIF 회전 정보를 픽셀에 반영
    # 미적용 시 브라우저(EXIF 자동 적용)와 백엔드 좌표계가 달라져 마스크 위치가 어긋남
    _pil = Image.open(io.BytesIO(contents)).convert("RGB")
    _pil = ImageOps.exif_transpose(_pil)
    image_np = np.array(_pil)

    # 친구 코드 셀 3 그대로 MAX_SIZE=1024
    MAX_SIZE = 1024
    h, w = image_np.shape[:2]
    scale = 1.0
    if max(h, w) > MAX_SIZE:
        scale = MAX_SIZE / max(h, w)
        image_np = cv2.resize(image_np, (int(w*scale), int(h*scale)))

    # 포인트도 같이 스케일 변환
    pts_raw = json.loads(points)
    pts = [[int(p[0]*scale), int(p[1]*scale)] for p in pts_raw]

    print(f"포인트 개수: {len(pts)}, 포인트: {pts}")

    # 친구 코드 셀 4 그대로
    result = sam2.predict(image_np, pts)

    h, w = image_np.shape[:2]
    print(f"[⏱ 처리시간] SAM2 세그멘테이션: {time.time()-_t0:.2f}초")
    return JSONResponse({
        "success": True,
        "image_size": {"width": w, "height": h},
        "score": result["score"],
        "mask_b64": image_to_base64(result["mask"]),
        "resized_image_b64": image_to_base64(image_np),
    })