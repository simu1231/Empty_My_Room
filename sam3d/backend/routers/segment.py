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
    res = request.app.state.residency

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

    # 친구 코드 셀 4 그대로.
    # use() 로 감싼 이유 둘: (1) 2 → 3 전이나 TTL 로 내려간 뒤 사용자가 새 사진을
    # 올리면 여기서 다시 올려야 한다, (2) 추론 중에 해제가 끼어들어 참조가
    # 끊기면 이 요청이 죽는다. 자세한 건 services/residency.py 머리말에.
    with res.use('sam2') as sam2:
        if sam2 is None:
            raise HTTPException(503, "SAM2 모델을 올릴 수 없습니다")
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

@router.post("/release")
async def release_models(request: Request):
    """SAM2/LaMa 를 내린다. 프런트가 2 → 3 단계 전이에서 한 번 부른다.

    2단계가 끝나면 둘은 그 세션에서 다시 쓰이지 않는다(3단계는 uLayout,
    4단계는 Omni3D + SAM3D). 그 1.25GB 를 쥐고 있으면 4단계에서 Omni3D 와
    SAM3D 가 동시에 돌 때 여유가 1.7GB 까지 떨어진다. 그래서 여기서 비운다.

    진행중인 요청이 있으면 아무것도 하지 않고 그렇게 알려준다 — 프런트가
    재시도할 필요는 없다. 안 내려가도 TTL 안전망이 결국 회수하고, 최악의
    결과는 "메모리를 조금 더 오래 쥐고 있다"뿐이다.
    """
    return JSONResponse({"success": True, **request.app.state.residency.release("2→3 단계 전이")})
