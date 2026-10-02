"""
uLayout 사이드카 기동 래퍼 — 웜업이 끝난 뒤에야 READY 로 표시한다.

왜 이 파일이 따로 있는가?
  ~/uLayout/server.py 는 이 저장소 밖에 있다(AMI 안에만 있고 컨테이너에는
  :ro 로 붙는다). bake 할 때 patch 로 덮는 방법도 있지만, 원본이 한 줄만
  움직여도 조용히 어긋난다. 그래서 고치는 대신 **이 래퍼를 대신 실행**하고
  server 모듈을 import 해서 기동 절차만 바꿔 끼운다. 원본은 읽기만 한다.

무엇을 바꾸나?
  원본은 startup 에서 모델만 올리고 바로 ulayout_loaded=true 를 돌려준다.
  그래서 워커가 "준비됐다" 고 믿고 첫 작업을 던지는데, 그 첫 요청이 CUDA
  컨텍스트 생성과 커널 로딩까지 혼자 떠안아 약 120초가 걸렸다.
  (워커의 SIDECAR_TIMEOUT 이 120초라 타임아웃 직전이었다.)

  여기서는 모델 로드 뒤에 더미 이미지로 /infer 경로를 한 번 통째로 돌리고,
  그게 성공한 뒤에야 server._net 을 세운다. /health 는 그 플래그를 보므로
  웜업이 끝나기 전에는 unhealthy 고, compose 의 depends_on(service_healthy)
  이 워커를 붙잡아 둔다 — SQS 폴링이 READY 뒤로 밀리는 건 이 경로다.

  웜업이 실패하면 _net 을 영영 안 세운다. 즉 이 컨테이너는 unhealthy 로
  남고 워커는 작업을 받지 않는다. 반쯤 망가진 GPU 가 작업을 집어삼키는 것보다
  아예 안 받는 편이 싸다.
"""
import os
import sys
import time
import traceback

import numpy as np

# server.py 는 ~/uLayout 안에 있다. 이 스크립트는 /app 에서 실행되므로
# sys.path[0] 이 /app 이다. 원본 디렉터리를 직접 넣어 준다.
ULAYOUT_DIR = os.environ.get(
    "ULAYOUT_DIR", os.path.join(os.environ.get("HOME", "/root"), "uLayout")
)
sys.path.insert(0, ULAYOUT_DIR)
os.chdir(ULAYOUT_DIR)  # 원본이 상대경로(ckpt/best_mp3d.pth)를 쓴다

# 더미 입력 해상도. 폰 사진이 리사이즈 없이 그대로 올라오므로(프론트·백엔드
# 어디에도 축소가 없다) 실제와 같은 크기로 덥혀야 PerspectiveFields 가 쓰는
# 커널까지 같이 올라온다. 'WxH'.
WARM_SIZE = os.environ.get("ULAYOUT_WARM_SIZE", "3024x4032")
WARM_CAMERA_HEIGHT_M = float(os.environ.get("ULAYOUT_WARM_CAMERA_HEIGHT_M", "1.6"))


def _dummy_room_rgb(width: int, height: int) -> np.ndarray:
    """
    방처럼 생긴 합성 이미지. 난수 노이즈를 쓰지 않는 이유는, 뒤쪽 치수 추정이
    경계선을 못 찾아 엉뚱하게 터지면 웜업 실패와 구분이 안 되기 때문이다.
    천장/벽/바닥 세 띠와 수직 모서리 두 개면 충분하다.
    """
    img = np.zeros((height, width, 3), dtype=np.uint8)
    ceiling_y = int(height * 0.25)
    floor_y = int(height * 0.70)
    img[:ceiling_y] = (235, 235, 230)          # 천장
    img[ceiling_y:floor_y] = (205, 200, 190)   # 벽
    img[floor_y:] = (150, 130, 110)            # 바닥
    for x in (int(width * 0.30), int(width * 0.70)):
        img[ceiling_y:floor_y, max(0, x - 2):x + 2] = (120, 115, 110)
    return img


def _warm_once(server, img_rgb) -> dict:
    """
    /infer 가 하는 일을 그대로 한 번 돈다. HTTP 를 거치지 않을 뿐
    PerspectiveFields → equirectangular 투영 → LGT-Net → 치수 추정까지
    같은 함수들을 탄다.
    """
    img_bgr, im_h, im_w, coord_pers, phi_boundary, equi_valid_cols, cam = (
        server._run_boundary_inference(img_rgb, WARM_CAMERA_HEIGHT_M)
    )
    # 여기까지가 GPU 경로다. 실패하면 진짜 고장이므로 위로 던진다.

    # 아래는 CPU 후처리다. 합성 사진이라 치수 추정이 의미 없는 값을 내거나
    # 터질 수 있는데, 그건 GPU 문제가 아니다. 경고만 남기고 READY 로 간다.
    try:
        server.estimate_room_dimensions(
            phi_boundary, equi_valid_cols, camera_height_m=WARM_CAMERA_HEIGHT_M
        )
        server.boundary_to_pixel_rows(coord_pers, phi_boundary, im_h, im_w)
        post_ok = True
    except Exception as e:
        print(f"[warmup] 후처리는 실패했지만 GPU 경로는 정상이다 — 넘어간다: {e}")
        post_ok = False

    server.torch.cuda.empty_cache()  # /infer 의 finally 와 같은 상태로 돌려놓는다
    return {"image_size": [im_w, im_h], "camera_params_deg": cam, "post_ok": post_ok}


def install(server, import_sec: float = 0.0) -> dict:
    """
    server 모듈의 기동 절차를 갈아끼우고 상태 dict 를 돌려준다.

    uvicorn.run 과 분리해 둔 이유는 테스트 때문이다. GPU 도 ~/uLayout 도 없는
    곳에서 가짜 server 를 넣어 '웜업이 실패하면 READY 가 안 된다'를 확인한다.
    """
    t_start = time.time() - import_sec
    t_import = import_sec

    # 원본이 등록해 둔 startup 핸들러(_load)를 걷어낸다. 그 안에서 _net 을
    # 바로 세워 버리기 때문에, 그대로 두면 웜업 전에 READY 가 돼 버린다.
    server.app.router.on_startup.clear()

    # /health 도 갈아끼운다. 라우트는 먼저 등록된 쪽이 이기므로 지우고 다시 단다.
    server.app.router.routes = [
        r for r in server.app.router.routes if getattr(r, "path", None) != "/health"
    ]

    state = {"stage": "starting", "import_sec": round(t_import, 2)}

    @server.app.get("/health")
    def health():
        # ulayout_loaded 는 '모델이 올라갔나' 가 아니라 '첫 요청을 지금 받아도
        # 되나' 를 뜻하도록 의미를 좁혔다.
        #
        # server._net 을 보면 안 된다. 웜업 자체가 그 전역을 읽어서 도는 탓에
        # 웜업 **중에도** 세워져 있고, 헬스체크는 10초마다 오므로 웜업이 끝나기
        # 전에 반드시 한 번은 true 를 보게 된다. 단계로 판단한다.
        return {"status": "ok", "ulayout_loaded": state["stage"] == "ready", **state}

    @server.app.on_event("startup")
    def _boot():
        try:
            t0 = time.time()
            state["stage"] = "loading"
            print(f"[boot] import {t_import:.1f}초 — 모델 로드 시작", flush=True)
            net = server.load_model()
            load_sec = time.time() - t0
            print(f"[boot] 모델 로드 {load_sec:.1f}초 — 웜업 시작 ({WARM_SIZE})", flush=True)

            t1 = time.time()
            state["stage"] = "warming"
            w, h = (int(v) for v in WARM_SIZE.lower().split("x"))
            # _run_boundary_inference 가 전역 _net 을 읽으므로 웜업 동안에도
            # 세워 둘 수밖에 없다. 그래서 READY 판정은 _net 이 아니라 stage 가
            # 한다(/health 주석 참고). 실패하면 아래에서 None 으로 되돌린다.
            server._net = net
            try:
                info = _warm_once(server, _dummy_room_rgb(w, h))
            except Exception:
                server._net = None  # 되돌린다. READY 로 가면 안 된다.
                raise
            warm_sec = time.time() - t1

            state.update(
                stage="ready",
                load_sec=round(load_sec, 2),
                warm_sec=round(warm_sec, 2),
                ready_sec=round(time.time() - t_start, 2),
                warm_image_size=info["image_size"],
                warm_post_ok=info["post_ok"],
            )
            print(
                f"[boot] 웜업 {warm_sec:.1f}초 — READY "
                f"(총 {state['ready_sec']:.1f}초 = import {t_import:.1f} + "
                f"로드 {load_sec:.1f} + 웜업 {warm_sec:.1f})",
                flush=True,
            )
        except Exception as e:
            state.update(stage="failed", error=str(e)[:500])
            traceback.print_exc()
            # 여기서 프로세스를 죽이지 않는다. 죽이면 compose 가 재시작시키고
            # 같은 실패를 반복하며 로그만 뒤덮는다. 떠 있되 unhealthy 로 남아
            # /health 로 이유를 읽을 수 있게 둔다.
            print(f"[boot] 웜업 실패 — READY 로 가지 않는다: {e}", flush=True)

    return state


def main() -> None:
    t0 = time.time()
    import server  # ~/uLayout/server.py — 읽기만 한다

    install(server, import_sec=time.time() - t0)
    server.uvicorn.run(
        server.app, host="0.0.0.0", port=int(os.environ.get("ULAYOUT_PORT", "8002"))
    )


if __name__ == "__main__":
    main()
