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

    # SD는 시작 시 로드하지 않는다.
    # SD(Stable Diffusion 1.5 inpainting + ControlNet, fp16)는 세션당 "빈방 만들기"에서
    # 딱 한 번(약 7.6초) 쓰이는데, 상주시키면 3~4GB를 계속 물고 있는다. RTX 4090
    # 24.5GB에 백엔드 + uLayout(8002) + Omni3D(8003)가 함께 올라가면 여유가 2.4GB까지
    # 떨어지고, 그 상태에서 프로세스 간 GPU 작업이 겹치면 SAM3D decode가 0.2초 →
    # 67초까지 튀는 걸 실측했다. inpaint 라우터가 필요할 때 로드하고 끝나면 해제한다.
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