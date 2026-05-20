import io
import time
import base64
import numpy as np
import cv2
from PIL import Image
from fastapi import APIRouter, UploadFile, File, Request, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

def np_to_b64(image_np):
    img_pil = Image.fromarray(image_np)
    buf = io.BytesIO()
    img_pil.save(buf, format="JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode()

@router.post("/remove")
async def remove_furniture(
    request: Request,
    image: UploadFile = File(...),
    mask:  UploadFile = File(...),
):
    lama = request.app.state.lama
    sd   = request.app.state.sd
    if lama is None:
        raise HTTPException(503, "LaMa 모델이 로드되지 않았습니다")

    img_bytes  = await image.read()
    mask_bytes = await mask.read()

    image_np = np.array(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
    mask_np  = np.array(Image.open(io.BytesIO(mask_bytes)).convert("L"))

    h, w = image_np.shape[:2]
    print(f"[DEBUG] image shape: {image_np.shape}, mask shape before resize: {mask_np.shape}")
    print(f"[DEBUG] mask unique values: {np.unique(mask_np)}, nonzero: {np.count_nonzero(mask_np)}")
    if mask_np.shape != (h, w):
        mask_np = cv2.resize(mask_np, (w, h), interpolation=cv2.INTER_NEAREST)
        print(f"[DEBUG] mask resized to: {mask_np.shape}, nonzero after resize: {np.count_nonzero(mask_np)}")

    # LaMa
    print("LaMa 시작...")
    _t0 = time.time()
    lama_result = lama.inpaint(image_np, mask_np)
    _lama_time = time.time() - _t0
    print(f"[⏱ 처리시간] LaMa 인페인팅: {_lama_time:.2f}초")

    # SD ControlNet
    _sd_time = 0.0
    if sd is not None:
        print("SD 시작...")
        _t1 = time.time()
        final_result = sd.inpaint(lama_result, mask_np)
        _sd_time = time.time() - _t1
        print(f"[⏱ 처리시간] SD Inpainting: {_sd_time:.2f}초")
    else:
        final_result = lama_result

    print(f"[⏱ 처리시간] 인페인팅 전체: {_lama_time + _sd_time:.2f}초")
    return JSONResponse({
        "success": True,
        "result_b64": np_to_b64(final_result),
    })