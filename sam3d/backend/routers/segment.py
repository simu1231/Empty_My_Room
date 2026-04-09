import io
import base64
import json
import numpy as np
import cv2
from PIL import Image
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
    sam2 = request.app.state.sam2
    if sam2 is None:
        raise HTTPException(503, "SAM2 모델이 로드되지 않았습니다")

    contents = await image.read()
    image_np = np.array(Image.open(io.BytesIO(contents)).convert("RGB"))

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
    return JSONResponse({
        "success": True,
        "image_size": {"width": w, "height": h},
        "score": result["score"],
        "mask_b64": image_to_base64(result["mask"]),
        "resized_image_b64": image_to_base64(image_np),
    })