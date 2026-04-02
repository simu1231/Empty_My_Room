import io
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
    if mask_np.shape != (h, w):
        mask_np = cv2.resize(mask_np, (w, h), interpolation=cv2.INTER_NEAREST)

    # 친구 코드 셀 5: LaMa 인페인팅
    print("LaMa 시작...")
    lama_result = lama.inpaint(image_np, mask_np)
    print("LaMa 완료!")

    # 친구 코드 셀 7: SD ControlNet
    if sd is not None:
        print("SD 시작...")
        final_result = sd.inpaint(lama_result, mask_np)
        print("SD 완료!")
    else:
        final_result = lama_result

    return JSONResponse({
        "success": True,
        "result_b64": np_to_b64(final_result),
    })