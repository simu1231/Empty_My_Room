"""
작업 명세 — API 와 워커가 **같은 파일**을 본다.

왜 공유하는가?
  검증 규칙이 두 군데로 갈라지면 "API 는 통과시켰는데 워커가 잘못된 입력이라며
  버리는" 조합이 생긴다. 사용자는 접수됐다는 응답을 받고 기다렸는데 결과는
  failed 다. 규칙을 한 파일에 두고 양쪽이 import 한다.

왜 하필 deploy/worker/ 에 있는가?
  worker 와 gpu 이미지의 빌드 컨텍스트가 이미 이 디렉터리라, 두 Dockerfile 은
  COPY 한 줄만 늘면 된다. API 쪽만 컨텍스트를 deploy/ 로 넓혀 가져간다.
  AMI 에 구워지는 쪽(gpu)을 안 건드리는 배치가 제일 싸다.

재시도 분류의 기준점이기도 하다. 여기서 InvalidJob 이 나면 "재시도해도 결과가
같다"는 뜻이므로, 워커는 메시지를 큐에 돌려보내지 않고 즉시 지운다.
"""


class InvalidJob(ValueError):
    """재시도해도 똑같이 실패하는 입력. 워커는 메시지를 즉시 삭제한다."""


# job_type → 반드시 있어야 하는 params 키. 빈 튜플이면 params 가 없어도 된다.
#
# room_layout 의 camera_height_m 은 **일부러** 넣지 않았다. worker.py 가
# 기본값 1.6 을 쓰므로 없어도 정상 동작한다. 필수가 아닌 걸 필수로 적으면
# 멀쩡한 작업이 400 으로 막힌다.
REQUIRED_PARAMS = {
    "sam3d_mesh":  (),
    "room_layout": (),
    "omni3d":      ("bbox", "category"),
}

JOB_TYPES = tuple(REQUIRED_PARAMS)


def validate(job_type, params=None, input_keys=None):
    """
    잘못된 작업이면 InvalidJob 을 던진다. 통과하면 아무것도 반환하지 않는다.

    API 는 제출 시점에, 워커는 S3 다운로드와 GPU 호출 **앞에서** 부른다.
    같은 함수라 두 곳의 판정이 어긋날 수 없다.
    """
    params = params or {}

    if job_type not in REQUIRED_PARAMS:
        raise InvalidJob(f"알 수 없는 job_type: {job_type!r} (가능: {', '.join(JOB_TYPES)})")

    # 빈 문자열도 누락으로 본다. 폼 전송에서는 값을 안 넣어도 키가 생길 수 있다.
    missing = [k for k in REQUIRED_PARAMS[job_type] if not str(params.get(k, "")).strip()]
    if missing:
        raise InvalidJob(f"{job_type} 작업에 필요한 파라미터 누락: {missing}")

    if job_type == "omni3d":
        _check_bbox(params["bbox"])

    # input_keys 를 안 넘기면(=API 가 파일 파트를 따로 검사하는 경우) 건너뛴다.
    if input_keys is not None and "image" not in input_keys:
        raise InvalidJob("image 입력이 필요합니다")


def _check_bbox(raw):
    """
    omni3d/server.py 가 bbox 를 'x1,y1,x2,y2' 로 받아 float 로 쪼갠다.
    여기서도 딱 그만큼만 본다 — 더 엄격하게 굴면 사이드카가 받아주는 입력을
    API 가 막아버린다. 넓이 검사만 더한다. 가로세로가 0 이하인 상자로는
    3D 박스를 만들 수 없어서, 재시도해도 결과가 같기 때문이다.
    """
    parts = str(raw).split(",")
    if len(parts) != 4:
        raise InvalidJob(f"bbox 는 'x1,y1,x2,y2' 네 값이어야 합니다: {raw!r}")
    try:
        x1, y1, x2, y2 = (float(p) for p in parts)
    except ValueError:
        raise InvalidJob(f"bbox 에 숫자가 아닌 값이 있습니다: {raw!r}") from None
    if x2 <= x1 or y2 <= y1:
        raise InvalidJob(f"bbox 의 넓이가 0 이하입니다: {raw!r}")


def is_non_retryable(exc) -> bool:
    """
    **화이트리스트**다. 여기 명시한 것만 즉시 삭제하고 나머지는 전부 재시도한다.

    반대로 짜면(= 재시도할 목록을 적고 나머지를 삭제) CUDA OOM 처럼 일시적인데
    처음 보는 오류가 재시도 없이 버려진다. 분류를 틀렸을 때 더 싼 쪽으로
    기울여야 한다 — 쓸데없는 재시도는 몇 분이지만, 잘못 버린 작업은 영영 없다.
    """
    if isinstance(exc, InvalidJob):
        return True

    # 사이드카가 4xx 를 주면 "이 입력으로는 안 된다"는 뜻이다. 5xx·타임아웃·
    # 커넥션 오류는 모델이나 GPU 사정이라 재시도할 가치가 있다.
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(code, int) and 400 <= code < 500
