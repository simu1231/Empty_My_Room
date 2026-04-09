import torch
import numpy as np
import torch.nn.functional as F
from gsplat import rasterization

class GaussianService:
    def __init__(self):
        self.device = 'cuda'
        print("Gaussian Splatting 서비스 로드 완료!")

    def get_camera_poses(self, num_views=6):
        # Zero123++ v1.2 정확한 카메라 파라미터
        elevations = [20, -10, 20, -10, 20, -10]
        azimuths   = [30, 90, 150, 210, 270, 330]
        poses = []

        for elev, azim in zip(elevations, azimuths):
            elev_rad = np.radians(elev)
            azim_rad = np.radians(azim)

            x = np.cos(elev_rad) * np.sin(azim_rad)
            y = np.sin(elev_rad)
            z = np.cos(elev_rad) * np.cos(azim_rad)

            cam_pos = np.array([x, y, z]) * 4.0

            forward = -cam_pos / np.linalg.norm(cam_pos)
            right = np.cross(forward, np.array([0, 1, 0]))
            right /= (np.linalg.norm(right) + 1e-8)
            up = np.cross(right, forward)

            R = np.stack([right, up, -forward], axis=1)
            w2c = np.eye(4)
            w2c[:3, :3] = R.T
            w2c[:3, 3] = -R.T @ cam_pos
            poses.append(w2c)

        return poses

    def images_to_gaussians(self, images, poses, init_points=None, init_colors=None, num_iterations=3000):
        H, W = images[0].shape[:2]

        # Zero123++ v1.2 FOV 30도
        fov = 30.0
        fx = fy = W / (2 * np.tan(np.radians(fov / 2)))
        cx, cy = W / 2.0, H / 2.0

        K = torch.tensor([[fx, 0, cx], [0, fy, cy], [0, 0, 1]],
                         dtype=torch.float32, device=self.device)

        if init_points is not None and len(init_points) > 0:
            means = torch.tensor(init_points, dtype=torch.float32, device=self.device)
            N = means.shape[0]
            if init_colors is not None:
                colors_init = torch.tensor(init_colors, dtype=torch.float32, device=self.device)
            else:
                colors_init = torch.rand(N, 3, dtype=torch.float32, device=self.device)
            print(f"SAM 3D 포인트 {N}개로 초기화!", flush=True)
        else:
            N = 10000
            means = torch.randn(N, 3, dtype=torch.float32, device=self.device) * 0.5
            colors_init = torch.rand(N, 3, dtype=torch.float32, device=self.device)

        means     = means.detach().requires_grad_(True)
        scales    = (torch.ones(N, 3, dtype=torch.float32, device=self.device) * -5.0).requires_grad_(True)
        quats     = torch.tensor([1.0, 0, 0, 0], dtype=torch.float32, device=self.device).repeat(N, 1).requires_grad_(True)
        opacities = (torch.ones(N, dtype=torch.float32, device=self.device) * -1.0).requires_grad_(True)
        colors    = torch.logit(colors_init.clamp(0.01, 0.99)).detach().requires_grad_(True)

        optimizer = torch.optim.Adam([
            {'params': [means],     'lr': 1e-2},
            {'params': [scales],    'lr': 1e-2},
            {'params': [quats],     'lr': 5e-3},
            {'params': [opacities], 'lr': 1e-1},
            {'params': [colors],    'lr': 5e-2},
        ])

        imgs_t   = [torch.from_numpy(i).float().to(self.device) / 255.0 for i in images]
        viewmats = [torch.from_numpy(p).float().to(self.device).unsqueeze(0) for p in poses]

        print(f"가우시안 학습 시작 ({num_iterations}회)...", flush=True)
        for step in range(num_iterations):
            optimizer.zero_grad()
            total_loss = torch.tensor(0.0, dtype=torch.float32, device=self.device)

            for vi in range(len(imgs_t)):
                renders, _, _ = rasterization(
                    means=means.float(),
                    quats=F.normalize(quats.float(), dim=-1),
                    scales=torch.exp(scales.float()),
                    opacities=torch.sigmoid(opacities.float()),
                    colors=torch.sigmoid(colors.float()),
                    viewmats=viewmats[vi].float(),
                    Ks=K.unsqueeze(0).float(),
                    width=W,
                    height=H,
                    packed=False,
                )
                total_loss = total_loss + F.l1_loss(renders[0], imgs_t[vi])

            total_loss.backward()
            optimizer.step()

            if step % 500 == 0:
                print(f"  step {step}/{num_iterations}, loss: {total_loss.item():.4f}", flush=True)

        print("가우시안 스플래팅 학습 완료!", flush=True)
        return {
            "means":       means.detach().cpu().numpy().tolist(),
            "colors":      torch.sigmoid(colors).detach().cpu().numpy().tolist(),
            "point_count": N,
        }