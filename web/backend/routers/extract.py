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

def pil_to_base64(pil_img):
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

@router.post("/furniture")
async def extract_furniture(
    request: Request,
    image: UploadFile = File(...),
    points: str = Form(...),
):
    sam2    = request.app.state.sam2
    extract = request.app.state.extract

    if sam2 is None:
        raise HTTPException(503, "SAM2 모델이 로드되지 않았습니다")

    contents = await image.read()
    image_np = np.array(Image.open(io.BytesIO(contents)).convert("RGB"))

    # MAX_SIZE=1024 리사이즈
    MAX_SIZE = 1024
    h, w = image_np.shape[:2]
    scale = 1.0
    if max(h, w) > MAX_SIZE:
        scale = MAX_SIZE / max(h, w)
        image_np = cv2.resize(image_np, (int(w*scale), int(h*scale)))

    pts_raw = json.loads(points)

    furniture_list = []

    # 친구 코드 셀 8 그대로 - 포인트마다 따로 추출
    for i, point in enumerate(pts_raw):
        scaled_point = [int(point[0]*scale), int(point[1]*scale)]

        # SAM2로 정밀 마스크 생성 (dilate 없이)
        input_points = np.array([scaled_point])
        input_labels = np.array([1])

        sam2.predictor.set_image(image_np)
        masks, scores, _ = sam2.predictor.predict(
            point_coords=input_points,
            point_labels=input_labels,
            multimask_output=True,
        )

        best_idx  = np.argmax(scores)
        best_mask = masks[best_idx].astype(bool)
        mask_raw  = (best_mask * 255).astype(np.uint8)

        # 친구 코드 셀 8 refine_mask 적용
        mask_refined = extract.refine_mask(mask_raw, edge_blur=3)

        # 가구 추출
        furniture_img, bbox = extract.extract_furniture(image_np, mask_refined)
        if furniture_img is None:
            continue

        furniture_list.append({
            "id":    int(i),
            "b64":   pil_to_base64(furniture_img),
            "score": float(scores[best_idx]),
            "bbox":  [int(x) for x in bbox],
        })

    return JSONResponse({
        "success":   True,
        "furniture": furniture_list,
    })