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

from routers import segment, inpaint
app.include_router(segment.router, prefix="/api/segment", tags=["Segment"])
app.include_router(inpaint.router, prefix="/api/inpaint", tags=["Inpaint"])

@app.get("/")
def root():
    return {"message": "Furniture Remover API 작동중!"}

@app.get("/health")
def health():
    return {
        "sam2": "loaded" if app.state.sam2 else "not_loaded",
        "lama": "loaded" if app.state.lama else "not_loaded",
        "sd":   "loaded" if app.state.sd   else "not_loaded",
    }