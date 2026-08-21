import os
import sys
os.environ['CUDA_HOME'] = os.environ.get('CONDA_PREFIX', '')
os.environ['LIDRA_SKIP_INIT'] = 'true'
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import pillow_heif
pillow_heif.register_heif_opener()  # 아이폰 HEIC/HEIF 업로드를 PIL.Image.open()에서 바로 열 수 있게 등록

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

    

    app.state.moge = None  # 온디맨드 로드 (GPU 메모리 충돌 방지)

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

from routers import segment, extract, sam3d, inpaint, room, omni3d
app.include_router(segment.router, prefix="/api/segment", tags=["Segment"])
app.include_router(extract.router, prefix="/api/extract", tags=["Extract"])
app.include_router(sam3d.router,   prefix="/api/sam3d",   tags=["SAM3D"])
app.include_router(inpaint.router, prefix="/api/inpaint", tags=["Inpaint"])
app.include_router(room.router, prefix="/api/room", tags=["Room"])
app.include_router(omni3d.router, prefix="/api/omni3d", tags=["Omni3D"])
@app.get("/")
def root():
    return {"message": "SAM3D Interior API 작동중!"}

@app.get("/health")
def health():
    return {
        "sam2":    "loaded" if getattr(app.state, 'sam2',    None) else "not_loaded",
        "lama":    "loaded" if getattr(app.state, 'lama',    None) else "not_loaded",
        "sd":      "loaded" if getattr(app.state, 'sd',      None) else "not_loaded",
        "extract": "loaded" if getattr(app.state, 'extract', None) else "not_loaded",
        "sam3d":   "loaded" if getattr(app.state, 'sam3d',   None) else "not_loaded",
    }