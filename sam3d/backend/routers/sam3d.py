"""
SAM3D 메쉬 생성 HTTP 라우터.

추론 본체는 services/sam3d_runner.py에 있다. 이 파일은 HTTP 입출력과
파이프라인 캐시/GPU 정리만 맡는다. 큐 워커(deploy/worker)도 같은 runner를
import하므로, 추론 로직을 고칠 때는 runner 한 곳만 고치면 된다.
"""
import gc

import torch
import orjson
from fastapi import APIRouter, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import Response

from services import sam3d_runner

router = APIRouter()

CACHE_SAM3D_PIPELINE = True


@router.post("/mesh")
async def generate_mesh(
    request: Request,
    image: UploadFile = File(...),
    category: str = Form(''),
):
    img_bytes = await image.read()
    pipeline = getattr(request.app.state, 'sam3d_pipeline', None) if CACHE_SAM3D_PIPELINE else None

    try:
        # 파이프라인 로드도 try 안에서 한다. 밖에 두면 로드 중 OOM이 났을 때
        # except/finally를 타지 않아 부분 할당된 텐서가 그대로 GPU에 남는다.
        if pipeline is None:
            pipeline = sam3d_runner.load_pipeline()
            if CACHE_SAM3D_PIPELINE:
                request.app.state.sam3d_pipeline = pipeline
                print("SAM3D 파이프라인 캐시 완료!")

        payload = sam3d_runner.generate(pipeline, img_bytes, category)
        return Response(content=orjson.dumps(payload), media_type="application/json")

    except Exception as e:
        import traceback
        traceback.print_exc()
        if CACHE_SAM3D_PIPELINE:
            request.app.state.sam3d_pipeline = None
        raise HTTPException(500, f"메쉬 생성 실패: {e}")
    finally:
        # 로컬 참조를 반드시 끊는다. 실패 시 app.state만 None으로 되돌리면 이 지역
        # 변수가 파이프라인을 계속 붙들고 있어서 아래 empty_cache()가 헛돈다
        # (실측: 실패 1회당 약 13.5GB가 GPU에 잔류해 재시작 전까지 복구 불가였음).
        # 성공한 경우엔 app.state가 객체를 참조하므로 캐시는 그대로 유지된다.
        pipeline = None
        gc.collect()
        torch.cuda.empty_cache()
        print("SAM3D GPU 캐시 정리 완료")
