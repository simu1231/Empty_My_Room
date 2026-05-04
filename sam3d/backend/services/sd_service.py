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

        # 친구 코드 셀 7 마스크 팽창 그대로
        mask_arr = np.array(mask_pil)
        mask_arr = cv2.dilate(mask_arr, np.ones((41, 41), np.uint8), iterations=2)
        kernel_down = np.zeros((40, 40), np.uint8)
        kernel_down[20:, :] = 1
        mask_arr = cv2.dilate(mask_arr, kernel_down, iterations=3)
        mask_arr = cv2.GaussianBlur(mask_arr, (51, 51), 0)
        mask_pil = Image.fromarray(mask_arr)

        # 친구 코드 셀 7 Canny 엣지 그대로
        image_cv = np.array(image_pil)
        edges    = cv2.Canny(image_cv, 100, 200)
        canny_pil = Image.fromarray(np.stack([edges]*3, axis=-1))

        # 친구 코드 셀 7 SD 실행 그대로
        result = self.pipe(
            prompt="empty room, continuous seamless wooden floor, clean flat white wall, photorealistic textures",
            negative_prompt="furniture, bed, chair, table, objects, 3d render, shadows, clutter, lines, bumps",
            image=image_pil,
            mask_image=mask_pil,
            control_image=canny_pil,
            num_inference_steps=50,
            guidance_scale=8.0,
            controlnet_conditioning_scale=1.0,
            strength=0.8,
        ).images[0]

        result = result.resize((W, H), Image.LANCZOS)
        return np.array(result)