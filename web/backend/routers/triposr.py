import io
import numpy as np
from PIL import Image
from fastapi import APIRouter, UploadFile, File, Request, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

@router.post("/mesh")
async def generate_mesh(
    request: Request,
    image: UploadFile = File(...),
):
    triposr = request.app.state.triposr
    if triposr is None:
        raise HTTPException(503, "TripoSR 모델이 로드되지 않았습니다")

    img_bytes = await image.read()
    image_pil = Image.open(io.BytesIO(img_bytes)).convert("RGBA")

    print("TripoSR 3D 메쉬 생성 중...")
    try:
        obj_path, mesh = triposr.image_to_mesh(image_pil)
        mesh_dict = triposr.mesh_to_dict(mesh)
        print(f"메쉬 생성 완료! 버텍스: {len(mesh_dict['vertices'])}개, 페이스: {len(mesh_dict['faces'])}개")
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"메쉬 생성 실패: {e}")
    
    return JSONResponse({
        "success":  True,
        "mesh":     mesh_dict,
        "vertices": len(mesh_dict["vertices"]),
        "faces":    len(mesh_dict["faces"]),
    })