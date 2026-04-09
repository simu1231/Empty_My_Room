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
    pipeline = request.app.state.sam3d
    if pipeline is None:
        raise HTTPException(503, "SAM3D 모델이 로드되지 않았습니다")

    img_bytes = await image.read()
    image_pil = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    image_np = np.array(image_pil)

    alpha = image_np[..., 3]
    rgb = image_np[..., :3]
    mask = alpha > 127

    print("Meta SAM3D 3D 메쉬 생성 중...")
    try:
        result = pipeline.run(
            rgb, mask, seed=42,
            stage1_only=False,
            with_mesh_postprocess=False,
            with_texture_baking=False,
            with_layout_postprocess=False,
            use_vertex_color=True,
            pointmap=None,
        )

        mesh = result.get('mesh')
        if mesh is None:
            raise HTTPException(500, "메쉬 생성 실패")

        if isinstance(mesh, list):
            mesh = mesh[0]

        vertices = mesh.vertices.cpu().numpy().tolist()
        faces = mesh.faces.cpu().numpy().tolist()
        print(f"메쉬 생성 완료! 버텍스: {len(vertices)}개, 페이스: {len(faces)}개")

        return JSONResponse({
            "success": True,
            "mesh": {
                "vertices": vertices,
                "faces": faces,
                "colors": [[0.8, 0.8, 0.8]] * len(vertices),
            },
            "vertices": len(vertices),
            "faces": len(faces),
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"메쉬 생성 실패: {e}")