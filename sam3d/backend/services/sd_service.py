import numpy as np
import cv2
import torch
import gc
from PIL import Image

class SDService:
    def __init__(self):
        self.pipe = None
        self._load()

    def _load(self):
        # 친구 코드 셀 7 그대로
        from diffusers import StableDiffusionControlNetInpaintPipeline, ControlNetModel, DDIMScheduler

        torch.cuda.empty_cache()
        gc.collect()

        print('Canny ControlNet 로드 중...')
        controlnet = ControlNetModel.from_pretrained(
            'lllyasviel/control_v11p_sd15_canny',
            torch_dtype=torch.float16
        )
        self.pipe = StableDiffusionControlNetInpaintPipeline.from_pretrained(
            'runwayml/stable-diffusion-inpainting',
            controlnet=controlnet,
            torch_dtype=torch.float16,
            safety_checker=None
        ).to('cuda')
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