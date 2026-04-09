import os
import sys
import torch
import numpy as np
from PIL import Image

ZERO123_DIR      = '/home/tmvlem5671/zero123plus'
ZERO123_PIPELINE = '/home/tmvlem5671/zero123plus/diffusers-support/pipeline.py'

class Zero123Service:
    def __init__(self):
        self.pipe = None
        self._load()

    def _load(self):
        sys.path.insert(0, ZERO123_DIR)
        from diffusers import DiffusionPipeline

        print("Zero123++ 로드 중...")
        self.pipe = DiffusionPipeline.from_pretrained(
            "sudo-ai/zero123plus-v1.2",
            custom_pipeline=ZERO123_PIPELINE,
            torch_dtype=torch.float16,
        ).to("cuda")
        print("Zero123++ 로드 완료!")

    def generate_views(self, image_pil):
        if image_pil.mode != 'RGBA':
            image_pil = image_pil.convert('RGBA')

        # 흰 배경으로 변환
        bg = Image.new('RGB', image_pil.size, (255, 255, 255))
        bg.paste(image_pil, mask=image_pil.split()[3])
        image_rgb = bg.resize((320, 320))

        with torch.no_grad():
            result = self.pipe(image_rgb, num_inference_steps=36)

        views = result.images[0]

        # 그리드 분할 (3x2)
        w, h = views.size
        single_w = w // 3
        single_h = h // 2

        view_list = []
        for row in range(2):
            for col in range(3):
                crop = views.crop((
                    col * single_w,
                    row * single_h,
                    (col + 1) * single_w,
                    (row + 1) * single_h,
                ))
                view_list.append(crop)

        return view_list