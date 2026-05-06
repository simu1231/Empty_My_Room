import io
import base64
import json
import numpy as np
import cv2
from PIL import Image
from collections import defaultdict
from fastapi import APIRouter, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

def pil_to_base64(pil_img):
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

@router.post("/furniture")
async def extract_furniture(
    request: Request,
    image: UploadFile = File(...),
    points: str = Form(...),
    labels: str = Form(...),
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

    pts_raw    = json.loads(points)
    labels_raw = json.loads(labels)

    # 예진이 코드 셀 8 그대로 - 레이블별 포인트 그룹화
    label_to_points = defaultdict(list)
    for pt, lbl in zip(pts_raw, labels_raw):
        label_to_points[lbl].append([int(pt[0]*scale), int(pt[1]*scale)])

    furniture_list = []

    # 이미지 인코딩은 가구 개수와 무관하게 1회만 수행
    sam2.predictor.set_image(image_np)

    for idx, (label, points_group) in enumerate(label_to_points.items()):
        print(f'[{idx+1}] "{label}" 처리 중 (포인트 {len(points_group)}개)...')

        # 예진이 코드 그대로 - 여러 포인트 한번에 전달
        masks, scores, _ = sam2.predictor.predict(
            point_coords=np.array(points_group),
            point_labels=np.array([1] * len(points_group)),
            multimask_output=True,
        )

        # 면적이 가장 큰 마스크 선택 (예진이 코드)
        areas = [np.sum(mask) for mask in masks]
        best_idx  = int(np.argmax(areas))
        best_mask = masks[best_idx].astype(bool)
        best_score = float(scores[best_idx])

        mask_raw = (best_mask * 255).astype(np.uint8)

        # 예진이 코드 - 구멍 메우기
        close_kernel = np.ones((25, 25), np.uint8)
        mask_raw = cv2.morphologyEx(mask_raw, cv2.MORPH_CLOSE, close_kernel)
        contours, _ = cv2.findContours(mask_raw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(mask_raw, contours, -1, 255, thickness=cv2.FILLED)

        mask_refined = extract.refine_mask(mask_raw, edge_blur=3)

        furniture_img, bbox = extract.extract_furniture(image_np, mask_refined)
        if furniture_img is None:
            continue

        furniture_list.append({
            "id":    idx,
            "name":  label,
            "b64":   pil_to_base64(furniture_img),
            "score": best_score,
            "bbox":  [int(x) for x in bbox],
        })

    return JSONResponse({
        "success":   True,
        "furniture": furniture_list,
    })