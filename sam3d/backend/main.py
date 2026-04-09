import os
import sys
os.environ['CUDA_HOME'] = os.environ.get('CONDA_PREFIX', '')
os.environ['LIDRA_SKIP_INIT'] = 'true'

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("SAM3D 서버 시작 - AI 모델 로드 중...")

    # SAM2 로드
    try:
        from services.sam2_service import SAM2Service
        app.state.sam2 = SAM2Service()
        print("SAM2 로드 완료!")
    except Exception as e:
        print(f"SAM2 로드 실패: {e}")
        app.state.sam2 = None

    # Extract 서비스 로드
    try:
        from services.extract_service import ExtractService
        app.state.extract = ExtractService()
        print("Extract 서비스 로드 완료!")
    except Exception as e:
        print(f"Extract 로드 실패: {e}")
        app.state.extract = None

    # Meta SAM3D 로드
    try:
        from omegaconf import OmegaConf
        from hydra.utils import instantiate
        PIPELINE_CONFIG = '/home/tmvlem5671/sam-3d-objects/checkpoints/hf/checkpoints/pipeline.yaml'
        config = OmegaConf.load(PIPELINE_CONFIG)
        config.rendering_engine = 'pytorch3d'
        config.compile_model = False
        config.workspace_dir = '/home/tmvlem5671/sam-3d-objects/checkpoints/hf/checkpoints'
        app.state.sam3d = instantiate(config)
        print("Meta SAM3D 로드 완료!")
    except Exception as e:
        print(f"Meta SAM3D 로드 실패: {e}")
        app.state.sam3d = None

    print("서버 준비 완료!")
    yield
    print("서버 종료")


app = FastAPI(title="SAM3D Interior API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from routers import segment, extract, sam3d
app.include_router(segment.router, prefix="/api/segment", tags=["Segment"])
app.include_router(extract.router, prefix="/api/extract", tags=["Extract"])
app.include_router(sam3d.router,   prefix="/api/sam3d",   tags=["SAM3D"])

@app.get("/")
def root():
    return {"message": "SAM3D Interior API 작동중!"}

@app.get("/health")
def health():
    return {
        "sam2":    "loaded" if getattr(app.state, 'sam2',    None) else "not_loaded",
        "extract": "loaded" if getattr(app.state, 'extract', None) else "not_loaded",
        "sam3d":   "loaded" if getattr(app.state, 'sam3d',   None) else "not_loaded",
    }
