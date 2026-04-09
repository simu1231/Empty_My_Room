import os
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("서버 시작 - AI 모델 로드 중...")

    # SAM2 로드
    try:
        from services.sam2_service import SAM2Service
        app.state.sam2 = SAM2Service()
        print("SAM2 로드 완료!")
    except Exception as e:
        print(f"SAM2 로드 실패: {e}")
        app.state.sam2 = None

    # LaMa 로드
    try:
        from services.lama_service import LamaService
        app.state.lama = LamaService()
        print("LaMa 로드 완료!")
    except Exception as e:
        print(f"LaMa 로드 실패: {e}")
        app.state.lama = None

    # SD 로드
    try:
        from services.sd_service import SDService
        app.state.sd = SDService()
        print("SD 로드 완료!")
    except Exception as e:
        print(f"SD 로드 실패: {e}")
        app.state.sd = None

    # Extract 서비스 로드
    try:
        from services.extract_service import ExtractService
        app.state.extract = ExtractService()
        print("Extract 서비스 로드 완료!")
    except Exception as e:
        print(f"Extract 로드 실패: {e}")
        app.state.extract = None

    # SAM3D 로드
    try:
        from services.sam3d_service import SAM3DService
        app.state.sam3d = SAM3DService()
        print("SAM3D 로드 완료!")
    except Exception as e:
        print(f"SAM3D 로드 실패: {e}")
        app.state.sam3d = None

    # Zero123 로드
    try:
        from services.zero123_service import Zero123Service
        app.state.zero123 = Zero123Service()
        print("Zero123++ 로드 완료!")
    except Exception as e:
        print(f"Zero123 로드 실패: {e}")
        app.state.zero123 = None

    # Gaussian 서비스 로드
    try:
        from services.gaussian_service import GaussianService
        app.state.gaussian = GaussianService()
        print("Gaussian 서비스 로드 완료!")
    except Exception as e:
        print(f"Gaussian 로드 실패: {e}")
        app.state.gaussian = None

    # TripoSR 로드
    try:
        from services.triposr_service import TripoSRService
        app.state.triposr = TripoSRService()
        print("TripoSR 로드 완료!")
    except Exception as e:
        print(f"TripoSR 로드 실패: {e}")
        app.state.triposr = None
        
    print("서버 준비 완료!")
    yield
    print("서버 종료")


app = FastAPI(title="Furniture Remover API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from routers import segment, inpaint, extract, sam3d, zero123, gaussian, triposr
app.include_router(segment.router,  prefix="/api/segment",  tags=["Segment"])
app.include_router(inpaint.router,  prefix="/api/inpaint",  tags=["Inpaint"])
app.include_router(extract.router,  prefix="/api/extract",  tags=["Extract"])
app.include_router(sam3d.router,    prefix="/api/sam3d",    tags=["SAM3D"])
app.include_router(zero123.router,  prefix="/api/zero123",  tags=["Zero123"])
app.include_router(gaussian.router, prefix="/api/gaussian", tags=["Gaussian"])
app.include_router(triposr.router, prefix="/api/triposr", tags=["TripoSR"])


@app.get("/")
def root():
    return {"message": "Furniture Remover API 작동중!"}

@app.get("/health")
def health():
    return {
        "sam2":    "loaded" if getattr(app.state, 'sam2',    None) else "not_loaded",
        "lama":    "loaded" if getattr(app.state, 'lama',    None) else "not_loaded",
        "sd":      "loaded" if getattr(app.state, 'sd',      None) else "not_loaded",
        "extract": "loaded" if getattr(app.state, 'extract', None) else "not_loaded",
        "sam3d":   "loaded" if getattr(app.state, 'sam3d',   None) else "not_loaded",
        "gaussian": "loaded" if getattr(app.state, 'gaussian', None) else "not_loaded",
        "triposr": "loaded" if getattr(app.state, 'triposr', None) else "not_loaded",
    }