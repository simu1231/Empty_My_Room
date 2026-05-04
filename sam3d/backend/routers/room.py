import io
import base64
import numpy as np
from PIL import Image
from fastapi import APIRouter, UploadFile, File
from fastapi.responses import JSONResponse

router = APIRouter()

@router.post("/extract-colors")
async def extract_room_colors(image: UploadFile = File(...)):
    img_bytes = await image.read()
    image_pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img_np = np.array(image_pil)
    
    h, w = img_np.shape[:2]
    
    # 바닥 색상 및 텍스처: 하단 25%
    floor_region = image_pil.crop((int(w*0.1), int(h*0.75), int(w*0.9), h))
    floor_np = np.array(floor_region)
    floor_color = (floor_np.mean(axis=(0,1)) / 255.0).tolist()

    # 벽 색상 및 텍스처: 중앙 상단
    wall_region = image_pil.crop((int(w*0.2), int(h*0.1), int(w*0.8), int(h*0.6)))
    wall_np = np.array(wall_region)
    wall_color = (wall_np.mean(axis=(0,1)) / 255.0).tolist()

    # 바닥 텍스처 이미지 (512x512로 리사이즈)
    floor_tex = floor_region.resize((512, 512), Image.LANCZOS)
    floor_buf = io.BytesIO()
    floor_tex.save(floor_buf, format='JPEG', quality=85)
    floor_tex_b64 = base64.b64encode(floor_buf.getvalue()).decode()

    # 벽 텍스처 이미지 (512x512로 리사이즈)
    wall_tex = wall_region.resize((512, 512), Image.LANCZOS)
    wall_buf = io.BytesIO()
    wall_tex.save(wall_buf, format='JPEG', quality=85)
    wall_tex_b64 = base64.b64encode(wall_buf.getvalue()).decode()

    return JSONResponse({
        "success": True,
        "floor_color": floor_color,
        "wall_color": wall_color,
        "floor_texture": floor_tex_b64,
        "wall_texture": wall_tex_b64,
    })
