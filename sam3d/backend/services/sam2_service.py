import os
import sys
import numpy as np
import cv2
import torch

SAM2_DIR = '/home/tmvlem5671/sam2_repo'

class SAM2Service:
    def __init__(self):
        self.predictor = None
        self._load()

    def _load(self):
        sys.path.insert(0, SAM2_DIR)
        os.chdir(SAM2_DIR)
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        sam2_model = build_sam2(
            'configs/sam2.1/sam2.1_hiera_l.yaml',
            f'{SAM2_DIR}/checkpoints/sam2.1_hiera_large.pt',
            device='cuda'
        )
        self.predictor = SAM2ImagePredictor(sam2_model)
        os.chdir('/home/tmvlem5671')
        print('SAM2 로드 완료!')

    def predict(self, image_np, points):
        # 친구 코드 셀 4 그대로
        # 포인트마다 따로 마스크 만들고 합치기
        combined_mask = np.zeros(image_np.shape[:2], dtype=np.uint8)

        # 이미지 인코딩 1회만 수행 (포인트 수와 무관)
        self.predictor.set_image(image_np)

        for point in points:
            input_points = np.array([point])
            input_labels = np.array([1])

            masks, scores, _ = self.predictor.predict(
                point_coords=input_points,
                point_labels=input_labels,
                multimask_output=True,
            )

            best_idx  = np.argmax(scores)
            best_mask = masks[best_idx].astype(bool)

            # 친구 코드 셀 4 dilate 그대로
            mask_np = (best_mask * 255).astype(np.uint8)
            kernel  = np.ones((40, 40), np.uint8)
            mask_np = cv2.dilate(mask_np, kernel, iterations=2)

            # 마스크 합치기
            combined_mask = np.maximum(combined_mask, mask_np)

        return {
            "mask": combined_mask,
            "score": 1.0,
        }