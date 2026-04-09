import os
import sys
import torch
import numpy as np
from PIL import Image

TRIPOSR_DIR = '/home/tmvlem5671/triposr'

class TripoSRService:
    def __init__(self):
        self.model  = None
        self.device = 'cuda'
        self._load()

    def _load(self):
        sys.path.insert(0, TRIPOSR_DIR)
        from tsr.system import TSR

        print("TripoSR 로드 중...")
        self.model = TSR.from_pretrained(
            "stabilityai/TripoSR",
            config_name="config.yaml",
            weight_name="model.ckpt",
        )
        self.model.renderer.set_chunk_size(131072)
        self.model.to(self.device)
        print("TripoSR 로드 완료!")

    def image_to_mesh(self, image_pil, output_path="/tmp/triposr_output"):
        """
        가구 이미지 → 3D 메쉬 생성 (전처리 강화 버전)
        """
        sys.path.insert(0, TRIPOSR_DIR)
        import rembg

        os.makedirs(output_path, exist_ok=True)

        # 1. 배경 제거 (이미 RGBA라면 그대로 사용, 아니면 rembg 실행)
        if image_pil.mode == 'RGBA':
            image = image_pil
        else:
            print("배경 제거(rembg) 실행 중...")
            image = rembg.remove(image_pil)

        # 2. 전처리 (찌그러짐 방지의 핵심)
        image = self._preprocess(image)

        # 디버그용 이미지 저장 (회색 배경에 소파가 중앙에 잘 있는지 확인용)
        image.save("/tmp/triposr_input_debug.png")
        print("디버그 이미지 저장: /tmp/triposr_input_debug.png")

        # 3. TripoSR 추론
        with torch.no_grad():
            # TripoSR은 0~1 사이의 float 텐서를 선호합니다.
            scene_codes = self.model([image], device=self.device)

        # 4. 메쉬 추출 
        # threshold가 너무 낮으면(10.0) 불필요한 노이즈가 붙고, 
        # 너무 높으면(25.0) 소파가 얇아집니다. 15.0~20.0 사이를 추천합니다.
        meshes = self.model.extract_mesh(
            scene_codes, 
            resolution=512, # 256보다 512가 훨씬 정교합니다.
            has_vertex_color=True, 
            threshold=25.0 
        )
        mesh = meshes[0]

        # 🔥 [추가] 고립된 노이즈(먼지) 제거 로직
        # 소파 본체와 연결되지 않고 공중에 떠 있는 작은 조각들을 지워줍니다.
        components = mesh.split(only_watertight=False)
        mesh = max(components, key=lambda x: x.vertices.shape[0])

        # OBJ 파일로 저장
        obj_path = os.path.join(output_path, "mesh.obj")
        mesh.export(obj_path)
        print(f"메쉬 저장 완료: {obj_path}")

        return obj_path, mesh

    def _preprocess(self, image, size=512):
        """
        TripoSR 전처리 최적화:
        1. 가구 영역만 타이트하게 크롭
        2. 중립 회색(127, 127, 127) 배경 사용 (입체감 향상)
        3. 정사각형 패딩 및 중앙 정렬
        """
        # 알파 채널을 이용해 실제 물체가 있는 영역(bbox) 계산
        if image.mode == 'RGBA':
            alpha = np.array(image.split()[-1])
            coords = np.argwhere(alpha > 0)
            if coords.size > 0:
                y_min, x_min = coords.min(axis=0)
                y_max, x_max = coords.max(axis=0)
                # 가구 부분만 크롭
                image = image.crop((x_min, y_min, x_max, y_max))

        # 가로세로 중 긴 쪽을 기준으로 캔버스 크기 결정
        w, h = image.size
        max_dim = max(w, h)
        
        # 가구가 캔버스의 약 80% 정도를 차지하도록 여백(Padding) 설정
        # 여백이 너무 없으면 찌그러집니다.
        new_size = int(max_dim * 1.2) 
        
        # 중립 회색 배경의 정사각형 이미지 생성
        # (TripoSR은 회색 배경에서 그림자와 형태를 더 잘 잡습니다)
        processed = Image.new('RGB', (new_size, new_size), (127, 127, 127))
        
        # 중앙 배치 좌표 계산
        paste_x = (new_size - w) // 2
        paste_y = (new_size - h) // 2
        
        if image.mode == 'RGBA':
            # 알파 마스크를 사용하여 회색 배경 위에 얹기
            processed.paste(image, (paste_x, paste_y), mask=image.split()[3])
        else:
            processed.paste(image, (paste_x, paste_y))

        # TripoSR 표준 입력 크기인 512x512로 리사이즈
        return processed.resize((size, size), Image.LANCZOS)

    def mesh_to_dict(self, mesh):
        """메쉬를 JSON 직렬화 가능한 딕셔너리로 변환"""
        vertices = mesh.vertices.tolist()
        faces    = mesh.faces.tolist()

        if hasattr(mesh, 'visual') and hasattr(mesh.visual, 'vertex_colors'):
            # vertex_colors는 (N, 4)이므로 RGB만 추출
            colors = (mesh.visual.vertex_colors[:, :3] / 255.0).tolist()
        else:
            colors = [[0.8, 0.8, 0.8]] * len(vertices)

        return {
            "vertices": vertices,
            "faces":    faces,
            "colors":   colors,
        }