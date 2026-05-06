import io
import gc
import torch
import numpy as np
from PIL import Image
from fastapi import APIRouter, UploadFile, File, Request, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

PIPELINE_CONFIG = '/home/tmvlem5671/sam-3d-objects/checkpoints/hf/checkpoints/pipeline.yaml'
WORKSPACE_DIR   = '/home/tmvlem5671/sam-3d-objects/checkpoints/hf/checkpoints'

# True: 파이프라인을 메모리에 유지 (재요청 시 로드 생략, 추가 VRAM 필요)
# False: 매 요청마다 로드/해제 (원래 동작, VRAM 절약)
CACHE_SAM3D_PIPELINE = False

@router.post("/mesh")
async def generate_mesh(
    request: Request,
    image: UploadFile = File(...),
):
    img_bytes = await image.read()
    image_pil = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    image_np = np.array(image_pil)

    alpha = image_np[..., 3]
    rgb = image_np[..., :3]
    mask = alpha > 127

    print("Meta SAM3D 3D 메쉬 생성 중...")

    pipeline = getattr(request.app.state, 'sam3d_pipeline', None) if CACHE_SAM3D_PIPELINE else None
    if pipeline is None:
        from omegaconf import OmegaConf
        from hydra.utils import instantiate
        config = OmegaConf.load(PIPELINE_CONFIG)
        config.rendering_engine = 'pytorch3d'
        config.compile_model = False
        config.workspace_dir = WORKSPACE_DIR
        pipeline = instantiate(config)
        if CACHE_SAM3D_PIPELINE:
            request.app.state.sam3d_pipeline = pipeline
            print("SAM3D 파이프라인 캐시 완료!")
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

        # 색상 추출
        if mesh.vertex_attrs is not None:
            colors = mesh.vertex_attrs[:, :3].cpu().numpy().tolist()
        else:
            colors = [[0.8, 0.8, 0.8]] * len(vertices)

        print(f"메쉬 생성 완료! 버텍스: {len(vertices)}개, 페이스: {len(faces)}개")

        return JSONResponse({
            "success": True,
            "mesh": {
                "vertices": vertices,
                "faces": faces,
                "colors": colors,
            },
            "vertices": len(vertices),
            "faces": len(faces),
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        if CACHE_SAM3D_PIPELINE:
            request.app.state.sam3d_pipeline = None  # 오류 시 캐시 초기화
        raise HTTPException(500, f"메쉬 생성 실패: {e}")
    finally:
        if not CACHE_SAM3D_PIPELINE:
            del pipeline
        gc.collect()
        torch.cuda.empty_cache()
        print("SAM3D GPU 캐시 정리 완료")