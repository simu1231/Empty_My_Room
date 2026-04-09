import io
import gc
import numpy as np
import torch
from PIL import Image
from fastapi import APIRouter, UploadFile, File, Request, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

# routers/gaussian.py

@router.post("/splat")
async def generate_gaussian_splat(request: Request, image: UploadFile = File(...)):
    gaussian = request.app.state.gaussian
    zero123  = request.app.state.zero123
    sam3d    = request.app.state.sam3d

    img_bytes = await image.read()
    image_pil = Image.open(io.BytesIO(img_bytes)).convert("RGBA")

    # 1. 배경 흰색 처리
    bg = Image.new('RGB', image_pil.size, (255, 255, 255))
    bg.paste(image_pil, mask=image_pil.split()[3])
    image_rgb = bg

    # 2. SAM 3D 포인트 추출 (PIL -> Numpy 변환 후 전달)
    print("SAM 3D 포인트 클라우드 추출 중...", flush=True)
    image_np = np.array(image_rgb)
    init_points, init_colors = sam3d.image_to_pointcloud(image_np)

    # 3. Zero123++ 6개 각도 이미지 생성
    print("Zero123++ 6개 각도 이미지 생성 중...", flush=True)
    views = zero123.generate_views(image_rgb)

    # 4. VRAM 확보 (메모리 비우기)
    torch.cuda.empty_cache()
    gc.collect()

    images_np = [np.array(v.convert("RGB")) for v in views]
    poses = gaussian.get_camera_poses(num_views=6)

    # 5. 가우시안 학습 실행 (데이터 타입 보정된 버전 호출)
    result = gaussian.images_to_gaussians(
        images=images_np, 
        poses=poses, 
        init_points=init_points, 
        init_colors=init_colors,
        num_iterations=10000,  # 더 긴 학습으로 품질 향상 시도
    )

    return JSONResponse({
        "success": True,
        "gaussians": result,
        "num_gaussians": result["point_count"],
    })