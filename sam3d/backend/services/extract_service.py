import numpy as np
import cv2
from PIL import Image

class ExtractService:

    def refine_mask(self, mask_np, edge_blur=3):
        # 친구 코드 셀 8 refine_mask 그대로
        kernel_small = np.ones((3, 3), np.uint8)
        mask_clean = cv2.morphologyEx(mask_np, cv2.MORPH_OPEN, kernel_small)

        mask_float = mask_clean.astype(np.float32) / 255.0
        blur_size  = edge_blur * 2 + 1
        mask_blur  = cv2.GaussianBlur(mask_float, (blur_size, blur_size), 0)

        inner = (mask_clean > 200).astype(np.float32)
        alpha = np.maximum(inner, mask_blur)

        return (alpha * 255).astype(np.uint8)

    def extract_furniture(self, image_np, mask_np):
        # 친구 코드 셀 8 extract_furniture 그대로
        h_img, w_img = image_np.shape[:2]
        if mask_np.shape != (h_img, w_img):
            mask_np = cv2.resize(mask_np, (w_img, h_img), interpolation=cv2.INTER_NEAREST)

        binary = mask_np > 127
        if not np.any(binary):
            return None, None

        rgba = np.zeros((h_img, w_img, 4), dtype=np.uint8)
        rgba[..., :3] = image_np
        rgba[..., 3]  = mask_np

        rows = np.any(binary, axis=1)
        cols = np.any(binary, axis=0)
        rmin, rmax = np.where(rows)[0][[0, -1]]
        cmin, cmax = np.where(cols)[0][[0, -1]]

        pad  = 5
        rmin = max(0, rmin - pad)
        rmax = min(h_img, rmax + pad)
        cmin = max(0, cmin - pad)
        cmax = min(w_img, cmax + pad)

        cropped = rgba[rmin:rmax, cmin:cmax]
        return Image.fromarray(cropped), (rmin, rmax, cmin, cmax)