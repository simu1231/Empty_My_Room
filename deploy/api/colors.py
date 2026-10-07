"""
방 바닥/벽의 평균 색과 텍스처를 뽑는다. GPU 가 필요 없는 유일한 2단계 작업이다.

■ 왜 여기로 옮겼는가
원래는 GPU 백엔드의 /extract-colors 에 있었다(sam3d/backend/routers/room.py).
그런데 하는 일이 crop + mean + resize + JPEG 인코딩뿐이다 — 토치도, CUDA 도,
모델도 안 쓴다. 이게 GPU 쪽에 있으면 **색 하나 뽑자고 g6.2xlarge 가 떠야
한다.** 시간당 $1.2 짜리 인스턴스를 7분 깨워서 $0.14 를 쓰는 셈이다.

여기(t4g.small, 상시 가동)로 옮기면 비용이 0 이고 응답이 즉시 온다.

■ numpy 를 안 쓴 이유
원본은 np.array(region).mean(axis=(0,1)) 이었다. 같은 값을 Pillow 의
ImageStat 이 준다. numpy 하나가 휠로 20MB 쯤 되는데, 이 API 이미지는
"토치를 안 넣는다"가 설계 원칙이라 그 원칙을 numpy 에도 적용한다.

ImageStat.Stat(img).mean 은 채널별 평균을 [R,G,B] 로 준다. 원본의
mean(axis=(0,1)) 과 **같은 축**이다(픽셀 전부를 평균, 채널은 유지).
"""
import base64
import io

from PIL import Image, ImageStat

# 원본 좌표. 바닥은 아래 25%, 벽은 가운데 위쪽이라고 가정한다. 휴리스틱이고
# 틀릴 수 있지만, 틀리는 방식이 기존과 **똑같아야** 프런트가 같은 결과를 본다.
FLOOR_BOX = (0.10, 0.75, 0.90, 1.00)
WALL_BOX  = (0.20, 0.10, 0.80, 0.60)
TEX_SIZE  = (512, 512)
TEX_QUALITY = 85


def _crop(img: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    w, h = img.size
    x0, y0, x1, y1 = box
    return img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))


def _mean01(region: Image.Image) -> list[float]:
    """0~255 평균을 0~1 로. 프런트가 THREE.Color 에 그대로 넣는 범위다."""
    return [c / 255.0 for c in ImageStat.Stat(region).mean]


def _jpeg_b64(region: Image.Image) -> str:
    buf = io.BytesIO()
    region.resize(TEX_SIZE, Image.LANCZOS).save(buf, format="JPEG", quality=TEX_QUALITY)
    return base64.b64encode(buf.getvalue()).decode()


def extract(img_bytes: bytes) -> dict:
    """GPU 백엔드 /extract-colors 와 **완전히 같은 키**로 돌려준다."""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    floor = _crop(img, FLOOR_BOX)
    wall  = _crop(img, WALL_BOX)
    return {
        "success": True,
        "floor_color": _mean01(floor),
        "wall_color":  _mean01(wall),
        "floor_texture": _jpeg_b64(floor),
        "wall_texture":  _jpeg_b64(wall),
    }
