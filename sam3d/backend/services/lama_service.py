import os
import sys
import numpy as np
import torch
from PIL import Image

LAMA_DIR  = '/home/tmvlem5671/lama_repo'
MODEL_DIR = '/home/tmvlem5671/lama_model'

class LamaService:
    def __init__(self):
        self.model  = None
        self.device = 'cuda'
        self._load()

    def _load(self):
        # 친구 코드 셀 2 그대로
        sys.path.insert(0, LAMA_DIR)
        from omegaconf import OmegaConf
        from saicinpainting.training.trainers import load_checkpoint

        print('LaMa 로드 중...')
        train_config = OmegaConf.load(f'{MODEL_DIR}/big-lama/config.yaml')
        train_config.training_model.predict_only = True
        train_config.visualizer.kind = 'noop'
        self.model = load_checkpoint(
            train_config,
            f'{MODEL_DIR}/big-lama/models/best.ckpt',
            strict=False,
            map_location='cpu'
        )
        self.model.freeze()
        self.model.to(self.device)
        print('LaMa 로드 완료!')

    def inpaint(self, image_np, mask_np):
        # 친구 코드 셀 5 run_lama 함수 그대로
        h, w = image_np.shape[:2]
        pad_h = (8 - h % 8) % 8
        pad_w = (8 - w % 8) % 8

        if pad_h > 0 or pad_w > 0:
            image_np = np.pad(image_np, ((0, pad_h), (0, pad_w), (0, 0)), mode='reflect')
            mask_np  = np.pad(mask_np,  ((0, pad_h), (0, pad_w)),          mode='reflect')

        img = image_np.astype(np.float32) / 255.0
        msk = (mask_np > 127).astype(np.float32)
        print(f"[DEBUG LaMa] mask_np unique: {np.unique(mask_np)}, nonzero: {np.count_nonzero(mask_np)}")
        print(f"[DEBUG LaMa] msk(float) sum: {msk.sum():.0f}, shape: {msk.shape}")
        img_t = torch.from_numpy(img).permute(2, 0, 1).unsqueeze(0).cuda()
        msk_t = torch.from_numpy(msk).unsqueeze(0).unsqueeze(0).cuda()

        with torch.no_grad():
            batch  = {'image': img_t, 'mask': msk_t}
            batch  = self.model(batch)
            torch.cuda.synchronize()  # GPU 비동기 연산 완료 대기 (정확한 시간 측정용)
            result = batch['inpainted'][0].permute(1, 2, 0).cpu().numpy()
            result = (result * 255).clip(0, 255).astype(np.uint8)

        diff = np.abs(result[:h, :w].astype(int) - image_np[:h, :w].astype(int)).mean()
        print(f"[DEBUG LaMa] 원본과 결과 평균 차이: {diff:.2f} (0이면 LaMa 미작동)")
        Image.fromarray(result[:h, :w]).save('/tmp/lama_debug.png')
        print("[DEBUG] LaMa 결과 저장완료: /tmp/lama_debug.png")

        return result[:h, :w]