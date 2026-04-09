import os
import sys
import torch
import numpy as np
import cv2
from PIL import Image

DEPTH_ANYTHING_DIR = '/home/tmvlem5671/depth_anything'

class SAM3DService:
    def __init__(self):
        self.depth_model = None
        self.transform    = None
        self._load()

    def _load(self):
        sys.path.insert(0, DEPTH_ANYTHING_DIR)
        from depth_anything.dpt import DepthAnything
        from depth_anything.util.transform import Resize, NormalizeImage, PrepareForNet
        from torchvision.transforms import Compose

        print("Depth Anything 로드 중...")
        self.depth_model = DepthAnything.from_pretrained(
            'LiheYoung/depth_anything_vitl14'
        ).to('cuda').eval()

        self.transform = Compose([
            Resize(
                width=518,
                height=518,
                resize_target=False,
                keep_aspect_ratio=True,
                ensure_multiple_of=14,
                resize_method='lower_bound',
                image_interpolation_method=cv2.INTER_CUBIC,
            ),
            NormalizeImage(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            PrepareForNet(),
        ])
        print("Depth Anything 로드 완료!")

    def get_depth(self, image_np):
        """RGB 이미지 → 깊이 맵"""
        image = image_np / 255.0
        sample = self.transform({'image': image})['image']
        sample = torch.from_numpy(sample).unsqueeze(0).cuda()

        with torch.no_grad():
            depth = self.depth_model(sample)

        depth = torch.nn.functional.interpolate(
            depth.unsqueeze(1),
            size=image_np.shape[:2],
            mode='bilinear',
            align_corners=False,
        ).squeeze().cpu().numpy()

        return depth

    def image_to_pointcloud(self, image_np, fx=500, fy=500, step=2):
        """RGB 이미지 → 포인트 클라우드"""
        depth_np = self.get_depth(image_np)
        h, w = depth_np.shape
        cx, cy = w / 2, h / 2

        points = []
        colors = []

        for v in range(0, h, step):
            for u in range(0, w, step):
                z = float(depth_np[v, u])
                if z <= 0:
                    continue
                x = (u - cx) * z / fx
                y = (v - cy) * z / fy
                points.append([x, y, z])
                colors.append(image_np[v, u] / 255.0)

        return np.array(points, dtype=np.float32), np.array(colors, dtype=np.float32)

    def segment_furniture(self, image_np, mask_np, depth_np=None, fx=500, fy=500):
        """마스크로 가구 포인트 클라우드 추출"""
        if depth_np is None:
            depth_np = self.get_depth(image_np)

        h, w = depth_np.shape
        cx, cy = w / 2, h / 2

        if mask_np.shape != (h, w):
            mask_np = cv2.resize(mask_np, (w, h), interpolation=cv2.INTER_NEAREST)

        binary = mask_np > 127
        points = []
        colors = []

        for v in range(0, h, 2):
            for u in range(0, w, 2):
                if not binary[v, u]:
                    continue
                z = float(depth_np[v, u])
                if z <= 0:
                    continue
                x = (u - cx) * z / fx
                y = (v - cy) * z / fy
                points.append([x, y, z])
                colors.append(image_np[v, u] / 255.0)

        if len(points) == 0:
            return None, None

        return np.array(points, dtype=np.float32), np.array(colors, dtype=np.float32)

    def merge_pointclouds(self, pointclouds):
        """여러 각도의 포인트 클라우드를 하나로 합치기"""
        all_points = []
        all_colors = []
        angles = [0, 60, 120, 180, 240, 300]

        for i, (points, colors) in enumerate(pointclouds):
            if points is None or len(points) == 0:
                continue
            angle_rad = np.radians(angles[i])
            cos_a = np.cos(angle_rad)
            sin_a = np.sin(angle_rad)
            rotation = np.array([
                [cos_a,  0, sin_a],
                [0,      1, 0    ],
                [-sin_a, 0, cos_a],
            ])
            rotated = points @ rotation.T
            all_points.append(rotated)
            all_colors.append(colors)

        if not all_points:
            return None, None

        return np.concatenate(all_points, axis=0), np.concatenate(all_colors, axis=0)

    def pointcloud_to_dict(self, points, colors):
        return {
            "points": points.tolist(),
            "colors": colors.tolist(),
        }