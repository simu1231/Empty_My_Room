import io
import base64
from PIL import Image
from fastapi import APIRouter, UploadFile, File, Request, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

def pil_to_base64(pil_img):
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

@router.post("/views")
async def generate_views(
    request: Request,
    image: UploadFile = File(...),
):
    """
    가구 이미지 → 6개 각도 이미지 생성
    """
    zero123 = request.app.state.zero123
    if zero123 is None:
        raise HTTPException(503, "Zero123++ 모델이 로드되지 않았습니다")

    contents = await image.read()
    image_pil = Image.open(io.BytesIO(contents)).convert("RGBA")

    try:
        print("Zero123++ 뷰 생성 중...")
        views = zero123.generate_views(image_pil)
        print(f"뷰 생성 완료! {len(views)}개")
    except Exception as e:
        raise HTTPException(500, f"뷰 생성 실패: {e}")

    return JSONResponse({
        "success": True,
        "views": [pil_to_base64(v) for v in views],
        "count": len(views),
    })