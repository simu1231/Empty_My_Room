import os
import time

import numpy as np
import cv2
import torch
import gc
from PIL import Image

# ── 가중치 위치 ────────────────────────────────────────────────────────────
# EMR_SD_PATH 가 있으면 **로컬 fp16 safetensors 트리**를 읽는다. 없으면 지금까지와
# 같이 HF 저장소 ID 로 읽는다(개발 PC 호환).
#
# ■ 왜 두 갈래로 두는가
# AWS 워커의 HF 캐시에는 fp32 pickle 만 있었다.
#       3.3G  unet/diffusion_pytorch_model.bin        ← fp32 pickle
#        470M  text_encoder/pytorch_model.bin
#        320M  vae/diffusion_pytorch_model.bin
# 여기에 `torch_dtype=torch.float16` 만 주면 **fp32 를 통째로 읽어** 메모리에서
# 캐스팅한다. CloudWatch 에서 첫 인페인팅 로드가 553.15초 / 532.53초로 찍혔다
# (서로 다른 인스턴스의 첫 호출 — 즉 인스턴스마다 한 번씩 낸다). 추론 자체는
# 12~15초라 로드가 40배다.
#
# tools/convert_sd_fp16.py 가 이 트리를 fp16 safetensors 로 미리 바꿔 AMI 에
# 넣는다. 읽을 바이트가 5.3GB → 2.7GB 로 줄고, pickle 역직렬화가 mmap 이 된다.
#
# 개발 PC 에서 EMR_SD_PATH 를 안 주면 예전 경로로 돌아간다. 로컬에서 변환을
# 안 돌린 사람의 환경이 깨지지 않아야 한다.
SD_PATH = os.environ.get('EMR_SD_PATH', '').strip()

# 변환 트리 안의 하위 디렉터리 이름. convert_sd_fp16.py 와 **짝이 맞아야 한다**.
_SD_SUBDIR = 'sd-inpainting'
_CN_SUBDIR = 'controlnet-canny'


def sd_weight_paths():
    """(파이프라인 경로, ControlNet 경로). 로컬 트리가 없으면 저장소 ID 를 준다.

    sd_warm.py 도 이 함수를 쓴다 — 선반입이 읽는 파일과 실제로 로드하는 파일이
    갈라지면, 엉뚱한 걸 따뜻하게 만들어 놓고 느린 이유를 찾게 된다.
    """
    if SD_PATH and os.path.isdir(SD_PATH):
        return (os.path.join(SD_PATH, _SD_SUBDIR),
                os.path.join(SD_PATH, _CN_SUBDIR))
    return ('runwayml/stable-diffusion-inpainting',
            'lllyasviel/control_v11p_sd15_canny')


class SDService:
    def __init__(self):
        self.pipe = None
        self._load()

    def _load(self):
        # 친구 코드 셀 7 그대로 — 가중치 경로만 sd_weight_paths() 로 바뀌었다.
        from diffusers import StableDiffusionControlNetInpaintPipeline, ControlNetModel, DDIMScheduler

        torch.cuda.empty_cache()
        gc.collect()

        sd_src, cn_src = sd_weight_paths()
        local = sd_src != 'runwayml/stable-diffusion-inpainting'
        print(f"SD 가중치: {'로컬 fp16' if local else 'HF 캐시(fp32)'} ← {sd_src}")

        print('Canny ControlNet 로드 중...')
        _t = time.time()
        controlnet = ControlNetModel.from_pretrained(
            cn_src,
            torch_dtype=torch.float16
        )
        print(f'[⏱ 처리시간] ControlNet 로드: {time.time() - _t:.2f}초')

        _t = time.time()
        self.pipe = StableDiffusionControlNetInpaintPipeline.from_pretrained(
            sd_src,
            controlnet=controlnet,
            torch_dtype=torch.float16,
            safety_checker=None
        ).to('cuda')
        print(f'[⏱ 처리시간] 파이프라인 로드: {time.time() - _t:.2f}초')
        self.pipe.enable_xformers_memory_efficient_attention() # xformers가 설치되어 있다면
        self.pipe.enable_attention_slicing() # VRAM 사용량 최적화
        self.pipe.scheduler = DDIMScheduler.from_config(self.pipe.scheduler.config)
        print('ControlNet 로드 완료!')

    def inpaint(self, lama_result_np, mask_np):
        # 친구 코드 셀 7 그대로
        image_pil = Image.fromarray(lama_result_np)
        mask_pil  = Image.fromarray(mask_np)

        W, H = image_pil.size
        new_w, new_h = (W // 8) * 8, (H // 8) * 8
        image_pil = image_pil.resize((new_w, new_h))
        mask_pil  = mask_pil.resize((new_w, new_h))

        mask_arr = np.array(mask_pil)
        mask_arr = cv2.dilate(mask_arr, np.ones((41, 41), np.uint8), iterations=2)
        kernel_down = np.zeros((40, 40), np.uint8)
        kernel_down[20:, :] = 1
        mask_arr = cv2.dilate(mask_arr, kernel_down, iterations=3)

        # 블러 전 선명한 마스크로 엣지 제거 (블러 후엔 경계가 흐릿해져 > 128 기준이 애매해짐)
        image_cv = np.array(image_pil)
        edges    = cv2.Canny(image_cv, 100, 200)
        edges[mask_arr > 128] = 0
        canny_pil = Image.fromarray(np.stack([edges]*3, axis=-1))

        # 엣지 제거 후 블러 적용
        mask_arr = cv2.GaussianBlur(mask_arr, (51, 51), 0)
        mask_pil = Image.fromarray(mask_arr)

        # 친구 코드 셀 7 SD 실행 그대로
        result = self.pipe(
            prompt="empty room interior, match existing wall color and floor material, seamless natural textures, photorealistic, no objects",
            negative_prompt="furniture, bed, chair, table, objects, 3d render, clutter, lines, bumps, color change, different material",
            image=image_pil,
            mask_image=mask_pil,
            control_image=canny_pil,
            num_inference_steps=50,
            guidance_scale=8.0,
            controlnet_conditioning_scale=0.5,
            strength=0.95,
            generator=torch.Generator("cuda").manual_seed(42),
        ).images[0]

        result = result.resize((W, H), Image.LANCZOS)
        return np.array(result)