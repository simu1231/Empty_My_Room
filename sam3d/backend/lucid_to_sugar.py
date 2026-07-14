"""
LucidDreamer → SuGaR 브릿지 스크립트

LucidDreamer의 gsplat.ply와 훈련 데이터를 SuGaR가 요구하는 형식으로 변환합니다.

사용법:
  python lucid_to_sugar.py \
    --lucid_out /path/to/lucid_output \   # LucidDreamer --save_dir 경로
    --sugar_scene /path/to/sugar_scene \  # SuGaR -s 입력 (새로 생성)
    --sugar_ckpt /path/to/sugar_ckpt      # SuGaR -c 입력 (새로 생성)

이후 SuGaR 실행:
  conda run -n sugar python /home/tmvlem5671/SuGaR/train_full_pipeline.py \
    -s /path/to/sugar_scene \
    --gs_output_dir /path/to/sugar_ckpt \
    -r dn_consistency --high_poly True --export_obj True
"""

import os
import json
import shutil
import argparse
import numpy as np
from pathlib import Path
from math import tan


def make_nerf_transforms_json(traindata: dict, images_dir: str) -> list[dict]:
    """
    LucidDreamer traindata['frames']의 transform_matrix를 그대로 사용.
    이미 OpenGL/Blender c2w 규격이므로 SuGaR Blender loader와 완전 호환.

    SuGaR dataset_readers.py readCamerasFromTransforms()가 하는 일:
      c2w[:3, 1:3] *= -1  (OpenGL → COLMAP 변환)
      w2c = inv(c2w)
      R = w2c[:3,:3].T
      T = w2c[:3, 3]
    → LucidDreamer의 transform_matrix는 이미 OpenGL 규격 → 추가 변환 불필요.
    """
    frames_json = []
    for i, frame in enumerate(traindata["frames"]):
        img_filename = f"frame_{i:04d}"
        img_path = os.path.join(images_dir, img_filename + ".png")

        # PIL Image 저장
        if "image" in frame and frame["image"] is not None:
            frame["image"].save(img_path)

        frames_json.append({
            "file_path": f"./images/{img_filename}",
            "transform_matrix": frame["transform_matrix"],
        })
    return frames_json


def save_point_cloud_ply(pcd_points: np.ndarray, pcd_colors: np.ndarray, out_path: str):
    """
    LucidDreamer의 pcd_points (3, N)과 pcd_colors (N, 3) [0,1]을
    SuGaR fetchPly()가 읽는 형식으로 저장.

    fetchPly()가 읽는 필드: x, y, z, nx, ny, nz, red, green, blue
    storePly()와 동일한 포맷으로 저장.

    normals은 zero로 초기화 — SuGaR는 Poisson reconstruction 전에
    open3d.estimate_normals()로 재계산하므로 문제 없음.
    """
    from plyfile import PlyData, PlyElement

    xyz = pcd_points.T  # (N, 3)
    rgb = np.clip(pcd_colors * 255.0, 0, 255).astype(np.uint8)  # (N, 3)
    normals = np.zeros_like(xyz, dtype=np.float32)

    dtype = [
        ("x", "f4"), ("y", "f4"), ("z", "f4"),
        ("nx", "f4"), ("ny", "f4"), ("nz", "f4"),
        ("red", "u1"), ("green", "u1"), ("blue", "u1"),
    ]
    elements = np.empty(xyz.shape[0], dtype=dtype)
    for col, name in zip([xyz[:, 0], xyz[:, 1], xyz[:, 2],
                          normals[:, 0], normals[:, 1], normals[:, 2],
                          rgb[:, 0], rgb[:, 1], rgb[:, 2]],
                         ["x", "y", "z", "nx", "ny", "nz", "red", "green", "blue"]):
        elements[name] = col

    ply_data = PlyData([PlyElement.describe(elements, "vertex")])
    ply_data.write(out_path)
    print(f"  Point cloud saved: {xyz.shape[0]:,} points → {out_path}")


def inspect_gsplat_ply_header(gsplat_path: str):
    """gsplat.ply의 PLY 헤더 필드를 출력합니다 (디버깅 용)."""
    from plyfile import PlyData
    data = PlyData.read(gsplat_path)
    print(f"\n=== PLY Header: {gsplat_path} ===")
    for elem in data.elements:
        print(f"  element {elem.name} ({len(elem.data):,} points)")
        for prop in elem.properties:
            print(f"    property {prop.val_dtype if hasattr(prop,'val_dtype') else ''} {prop.name}")
    return data


def make_cameras_json(frames_json: list, camera_angle_x: float, W: int, H: int) -> list:
    """
    SuGaR의 gs_model.py가 필요로 하는 cameras.json 형식 생성.

    vanilla 3DGS는 훈련 후 cameras.json을 자동 저장하지만,
    LucidDreamer checkpoint를 사용할 때는 직접 생성해야 한다.

    cameras.json 형식:
      [{"img_name": "frame_0000",
        "rotation": [[r00,r01,r02],[r10,r11,r12],[r20,r21,r22]],  # W2C 3x3
        "position": [t0,t1,t2],  # W2C translation (NOT camera position in world)
        "width": 512, "height": 512, "fy": ..., "fx": ...}, ...]

    transforms_train.json의 transform_matrix는 OpenGL c2w 형식.
    SuGaR Blender loader 변환: c2w[:3,1:3] *= -1 → inv → W2C
    cameras.json은 이 W2C를 직접 저장.
    """
    focal = W / (2 * np.tan(camera_angle_x / 2))
    cameras = []
    for i, frame in enumerate(frames_json):
        img_name = Path(frame["file_path"]).stem
        c2w = np.array(frame["transform_matrix"])
        # SuGaR Blender loader 동일 변환 적용: OpenGL → COLMAP
        c2w_colmap = c2w.copy()
        c2w_colmap[:3, 1:3] *= -1
        w2c = np.linalg.inv(c2w_colmap)
        cameras.append({
            "id": i,
            "img_name": img_name,
            "rotation": w2c[:3, :3].tolist(),
            "position": w2c[:3, 3].tolist(),
            "width": W,
            "height": H,
            "fx": float(focal),
            "fy": float(focal),
        })
    return cameras


def convert(lucid_out_dir: str, sugar_scene_dir: str, sugar_ckpt_dir: str,
            lucid_traindata: dict = None):
    """
    주 변환 함수.

    lucid_traindata: LucidDreamer의 traindata dict.
                     None이면 lucid_out_dir에서 직접 파일만 처리
                     (카메라/이미지는 LucidDreamer 실행 중에만 in-memory임).
    """
    os.makedirs(sugar_scene_dir, exist_ok=True)
    os.makedirs(sugar_ckpt_dir, exist_ok=True)

    # ── 1. SuGaR 체크포인트: gsplat.ply → point_cloud/iteration_30000/point_cloud.ply
    gsplat_src = os.path.join(lucid_out_dir, "gsplat.ply")
    assert os.path.exists(gsplat_src), f"gsplat.ply not found: {gsplat_src}"

    # PLY 헤더 정보 출력
    inspect_gsplat_ply_header(gsplat_src)

    # SuGaR train.py의 --iteration_to_load 기본값=7000
    # → point_cloud/iteration_7000/point_cloud.ply 로 저장
    ckpt_ply_dir = os.path.join(sugar_ckpt_dir, "point_cloud", "iteration_7000")
    os.makedirs(ckpt_ply_dir, exist_ok=True)
    ckpt_ply_dst = os.path.join(ckpt_ply_dir, "point_cloud.ply")
    shutil.copy2(gsplat_src, ckpt_ply_dst)
    print(f"\n[1/4] Checkpoint PLY: {ckpt_ply_dst}")

    if lucid_traindata is None:
        print("\n[!] traindata가 없습니다. in-memory 데이터 (카메라/이미지)를 변환할 수 없습니다.")
        print("    LucidDreamer 실행 후 --save_traindata 옵션으로 재실행하거나,")
        print("    대안 경로(COLMAP)를 사용하세요.")
        return

    # ── 2. 이미지 저장
    images_dir = os.path.join(sugar_scene_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    print(f"\n[2/4] Saving {len(lucid_traindata['frames'])} training images → {images_dir}")
    frames_json = make_nerf_transforms_json(lucid_traindata, images_dir)
    print(f"  Done: {len(frames_json)} frames")

    # ── 3. transforms_train.json / transforms_test.json
    transforms = {
        "camera_angle_x": lucid_traindata["camera_angle_x"],
        "frames": frames_json,
    }
    train_json_path = os.path.join(sugar_scene_dir, "transforms_train.json")
    test_json_path = os.path.join(sugar_scene_dir, "transforms_test.json")
    with open(train_json_path, "w") as f:
        json.dump(transforms, f, indent=2)
    # test set: 빈 리스트 (SuGaR eval=False일 때 무시됨)
    test_transforms = {"camera_angle_x": lucid_traindata["camera_angle_x"], "frames": []}
    with open(test_json_path, "w") as f:
        json.dump(test_transforms, f, indent=2)
    print(f"\n[3/4] Camera transforms: {train_json_path}")
    print(f"      camera_angle_x = {lucid_traindata['camera_angle_x']:.4f} rad "
          f"({np.degrees(lucid_traindata['camera_angle_x']):.1f}°)")
    print(f"      image size: {lucid_traindata['W']} × {lucid_traindata['H']}")

    # ── 3b. cameras.json — SuGaR gs_model.py가 요구하는 파일
    #        (vanilla 3DGS가 자동 생성하지만, LucidDreamer 체크포인트 사용 시 직접 생성)
    cameras_json_path = os.path.join(sugar_ckpt_dir, "cameras.json")
    cameras_data = make_cameras_json(
        frames_json,
        lucid_traindata["camera_angle_x"],
        lucid_traindata["W"],
        lucid_traindata["H"],
    )
    with open(cameras_json_path, "w") as f:
        json.dump(cameras_data, f, indent=2)
    print(f"      cameras.json: {cameras_json_path} ({len(cameras_data)} cameras)")

    # ── 4. 포인트 클라우드 (초기화용)
    pcd_ply_path = os.path.join(sugar_scene_dir, "points3d.ply")
    print(f"\n[4/4] Saving point cloud → {pcd_ply_path}")
    save_point_cloud_ply(
        lucid_traindata["pcd_points"],  # (3, N)
        lucid_traindata["pcd_colors"],  # (N, 3) [0,1]
        pcd_ply_path,
    )

    print("\n" + "="*60)
    print("변환 완료! SuGaR 실행 명령어:")
    print(f"""
  source ~/miniconda3/etc/profile.d/conda.sh && conda activate sugar
  export CUDA_HOME=~/miniconda3/envs/sugar
  export PATH=$CUDA_HOME/bin:$PATH
  cd /home/tmvlem5671/SuGaR
  python train_full_pipeline.py \\
    -s {sugar_scene_dir} \\
    --gs_output_dir {sugar_ckpt_dir} \\
    -r dn_consistency \\
    --high_poly True \\
    --export_obj True \\
    --eval False \\
    --refinement_time short
""")


def patch_luciddreamer_for_bridge(lucid_dir: str):
    """
    LucidDreamer 실행 후 traindata를 pickle로 저장하는 패치 스크립트를 출력합니다.
    실제 패치는 luciddreamer.py의 create() 메서드 끝에 삽입해야 합니다.
    """
    patch = f"""
# luciddreamer.py create() 메서드 끝, self.save_ply() 호출 직전에 추가:
import pickle, os
_traindata_path = os.path.join(self.save_dir, 'traindata.pkl')
_traindata_save = dict(self.traindata)
_traindata_save['frames'] = [
    {{'transform_matrix': f['transform_matrix'],
      'image': f['image']}}
    for f in self.traindata['frames']
]
with open(_traindata_path, 'wb') as _f:
    pickle.dump(_traindata_save, _f)
print(f'traindata saved → {{_traindata_path}}')
"""
    print(patch)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LucidDreamer → SuGaR 브릿지")
    parser.add_argument("--lucid_out", required=True, help="LucidDreamer --save_dir 경로")
    parser.add_argument("--sugar_scene", required=True, help="SuGaR -s 씬 디렉토리 (새로 생성)")
    parser.add_argument("--sugar_ckpt", required=True, help="SuGaR -c 체크포인트 디렉토리 (새로 생성)")
    parser.add_argument("--traindata_pkl", default=None,
                        help="LucidDreamer traindata.pkl 경로 (선택, in-memory 카메라/이미지 복원)")
    parser.add_argument("--inspect_only", action="store_true",
                        help="gsplat.ply 헤더만 출력하고 종료")
    args = parser.parse_args()

    if args.inspect_only:
        inspect_gsplat_ply_header(os.path.join(args.lucid_out, "gsplat.ply"))
        raise SystemExit(0)

    traindata = None
    if args.traindata_pkl and os.path.exists(args.traindata_pkl):
        import pickle
        with open(args.traindata_pkl, "rb") as f:
            traindata = pickle.load(f)
        print(f"traindata loaded: {len(traindata['frames'])} frames")

    convert(args.lucid_out, args.sugar_scene, args.sugar_ckpt, traindata)
