import requests
from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse

router = APIRouter()

OMNI3D_SIDECAR_URL = 'http://localhost:8003/estimate'


@router.post("/estimate")
async def estimate_furniture_size(
    image: UploadFile = File(...),
    bbox: str = Form(...),
    category: str = Form(...),
):
    """
    Omni3D 사이드카(별도 conda env, localhost:8003)로 (전체 사진, bbox, 한글 카테고리)를
    보내 oracle2D 주입 기반 실제 크기(m)를 추정해서 돌려준다. Omni3D 미지원 카테고리
    (시계/화분/꽃)는 사이드카가 success:false, unsupported:true로 응답 — 프론트는 SAM3D
    (MoGe) 기반 sam3d_size_m 폴백으로 넘어간다. 사이드카가 꺼져있으면 success:false로
    응답해 같은 폴백 경로를 타게 한다.
    """
    img_bytes = await image.read()
    try:
        resp = requests.post(
            OMNI3D_SIDECAR_URL,
            files={"image": (image.filename or "photo.jpg", img_bytes, image.content_type or "image/jpeg")},
            data={"bbox": bbox, "category": category},
            timeout=30,
        )
        resp.raise_for_status()
        return JSONResponse(resp.json())
    except requests.exceptions.RequestException as e:
        return JSONResponse(
            {"success": False, "error": f"Omni3D 서버에 연결할 수 없습니다: {e}"},
            status_code=503,
        )
