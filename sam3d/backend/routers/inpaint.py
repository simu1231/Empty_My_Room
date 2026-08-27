import io
import os
import gc
import time
import base64
import numpy as np
import cv2
from PIL import Image
from fastapi import APIRouter, UploadFile, File, Request, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

EMPTY_ROOM_SAVE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'empty_room.jpg')

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

    # SD ControlNet — 온디맨드 로드 후 사용이 끝나면 곧바로 해제한다.
    # 이 단계는 세션당 한 번이고, 바로 다음 단계(방 분석 → 3D 변환)가 GPU를 훨씬 많이
    # 쓰기 때문에 3~4GB를 계속 붙들고 있을 이유가 없다. 로드 실패 시에는 종전처럼
    # LaMa 결과만 그대로 사용한다(품질만 조금 떨어지고 동작은 유지).
    _sd_time = 0.0
    final_result = lama_result
    sd = None
    try:
        from services.sd_service import SDService
        print("SD 온디맨드 로드 중...")
        _t_load = time.time()
        sd = SDService()
        print(f"[⏱ 처리시간] SD 로드: {time.time()-_t_load:.2f}초")
    except Exception as e:
        print(f"SD 로드 실패 — LaMa 결과만 사용: {e}")

    if sd is not None:
        try:
            print("SD 시작...")
            _t1 = time.time()
            final_result = sd.inpaint(lama_result, mask_np)
            _sd_time = time.time() - _t1
            print(f"[⏱ 처리시간] SD Inpainting: {_sd_time:.2f}초")
        except Exception as e:
            print(f"SD 추론 실패 — LaMa 결과만 사용: {e}")
            final_result = lama_result
        finally:
            del sd
            gc.collect()
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
            print("SD 언로드 완료 (GPU 메모리 반환)")

    print(f"[⏱ 처리시간] 인페인팅 전체: {_lama_time + _sd_time:.2f}초")

    os.makedirs(os.path.dirname(EMPTY_ROOM_SAVE_PATH), exist_ok=True)
    Image.fromarray(final_result).save(EMPTY_ROOM_SAVE_PATH, quality=95)
    print(f"[DEBUG] 빈 방 이미지 저장: {EMPTY_ROOM_SAVE_PATH}")

    return JSONResponse({
        "success": True,
        "result_b64": np_to_b64(final_result),
    })