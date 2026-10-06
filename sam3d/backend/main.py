import asyncio
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

    # SAM2/LaMa 상주 관리. 위에서 이미 올려뒀으므로 residency 는 그걸 인수받아
    # 쓰기만 한다 — 첫 클릭을 빠르게 하려고 시작 로드는 그대로 둔다.
    # 왜 해제가 필요한지, 왜 TTL 을 600초로 길게 두는지는 residency.py 머리말에.
    from services.residency import SegmentResidency, SWEEP_SEC
    app.state.residency = SegmentResidency(app)

    async def _sweep_loop(res):
        while True:
            await asyncio.sleep(SWEEP_SEC)
            try:
                res.sweep()
            except Exception as e:
                print(f"[residency] 스위퍼 오류(무시하고 계속): {e}")

    sweeper = None
    if app.state.residency.ttl_sec > 0:
        sweeper = asyncio.create_task(_sweep_loop(app.state.residency))
        print(f"TTL 안전망 {app.state.residency.ttl_sec:.0f}초 (점검 {SWEEP_SEC:.0f}초 간격)")

    # 세션 상태 알림. 기본은 꺼져 있다 — 왜 꺼 두는지는 session_state.py 머리말에.
    from services.session_state import SessionState, WRITE_SEC
    app.state.session = SessionState()

    async def _state_loop(st):
        while True:
            st.write()
            await asyncio.sleep(WRITE_SEC)

    stater = None
    if app.state.session.enabled:
        stater = asyncio.create_task(_state_loop(app.state.session))
        print(f"세션 상태 알림 → {app.state.session.path} "
              f"(활동 유효 {app.state.session.busy_sec:.0f}초, {WRITE_SEC:.0f}초 간격)")

    print("서버 준비 완료!")
    yield
    for t in (sweeper, stater):
        if t is not None:
            t.cancel()
    print("서버 종료")

app = FastAPI(title="SAM3D Interior API", lifespan=lifespan)

# 허용 출처. 기본값은 지금까지와 같은 "*" 라 동작이 달라지지 않는다.
# 환경변수로 뺀 이유는 좁히고 싶어서가 아니라 **좁힐 때 재굽기를 안 하기
# 위해서**다. userdata.sh 는 저장소를 다시 받지 않는다(git pull 이 없다) —
# 이 파일은 bootstrap-ami.sh 의 clone 시점에 AMI 안으로 굳는다. 값이 코드에
# 박혀 있으면 출처 한 줄을 고치는 데 AMI 를 다시 구워야 한다.
# 좁힐 때: 시작 템플릿에서 EMR_CORS_ORIGINS="https://내도메인" 을 넣는다.
_origins = [o.strip() for o in os.environ.get('EMR_CORS_ORIGINS', '*').split(',') if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 모든 요청을 "활동"으로 센다. 어느 엔드포인트가 세션을 뜻하는지 고르지
# 않는 이유는, 고르는 순간 새 엔드포인트가 생길 때마다 여기를 같이 고쳐야
# 하고 빠뜨리면 세션 한가운데서 인스턴스가 내려가기 때문이다. 헬스체크는
# 뺀다 — 도커 헬스체크가 10초마다 때리므로 넣으면 영원히 busy 가 된다.
@app.middleware("http")
async def _mark_activity(request, call_next):
    st = getattr(app.state, 'session', None)
    if st is not None and request.url.path not in ('/health', '/'):
        st.touch()
    return await call_next(request)


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
        "residency": getattr(app.state, 'residency', None).status()
                     if getattr(app.state, 'residency', None) else None,
        # 세션 알림이 켜져 있는지. 배포에서 "리퍼가 왜 안 내려가나 / 왜 세션
        # 중에 내려갔나"를 물을 때 제일 먼저 볼 값이라 밖으로 뺀다.
        "session": ({"state_file": app.state.session.path,
                     "busy_sec": app.state.session.busy_sec}
                    if getattr(app.state, 'session', None)
                    and app.state.session.enabled else None),
    }