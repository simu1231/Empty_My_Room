import numpy as np
import cv2
import json
import os
import pandas as pd
from services.sam2_service import SAM2Service

BASE_DIR    = "/home/tmvlem5671/Empty_My_Room/web/backend/"
IMAGE_DIR   = BASE_DIR + "eval_data/images/"
GT_JSON_DIR = BASE_DIR + "eval_data/gt_labels/"
THRESHOLD   = 0.8

FURNITURE_MAP = {
    "bed":   "침대",
    "chair": "의자",
    "desk":  "책상",
}

CLICK_POINTS = {
    "chair_01": [[512, 390]],
    "chair_02": [[480, 320], [480, 400], [480, 450]],
    "chair_03": [[490, 300], [490, 380], [490, 430]],
    "chair_04": [[780, 380]],
    "desk_01":  [[512, 360]],
    "desk_02":  [[512, 360]],
    "desk_03":  [[512, 350]],
    "desk_04":  [[512, 350]],
    "bed_01":   [[1408, 900]],
    "bed_02":   [[512, 300], [512, 400], [400, 350]],
    "bed_03":   [[512, 300], [512, 400], [400, 350]],
    "bed_04":   [[512, 280], [512, 380], [300, 330]],
}

def find_image(img_id):
    for ext in [".jpg", ".png", ".jpeg"]:
        path = f"{IMAGE_DIR}{img_id}{ext}"
        if os.path.exists(path):
            return path
    return None

def labelme_to_mask(json_path, image_shape):
    with open(json_path) as f:
        data = json.load(f)
    mask = np.zeros(image_shape[:2], dtype=np.uint8)
    for shape in data["shapes"]:
        pts = np.array(shape["points"], dtype=np.int32)
        cv2.fillPoly(mask, [pts], 1)
    return mask

def calc_coverage(sam_mask, gt_mask):
    sam_binary = (sam_mask > 127).astype(np.uint8)
    gt_area = gt_mask.sum()
    if gt_area == 0:
        return 0.0
    covered = (sam_binary & gt_mask).sum()
    return float(covered / gt_area)

def calc_precision(sam_mask, gt_mask):
    sam_binary = (sam_mask > 127).astype(np.uint8)
    sam_area = sam_binary.sum()
    if sam_area == 0:
        return 0.0
    covered = (sam_binary & gt_mask).sum()
    return float(covered / sam_area)

def main():
    sam2 = SAM2Service()
    results = []
    os.makedirs("eval_data/vis", exist_ok=True)

    for eng, kor in FURNITURE_MAP.items():
        for i in range(1, 5):
            img_id   = f"{eng}_{i:02d}"
            gt_path  = f"{GT_JSON_DIR}{img_id}.json"
            img_path = find_image(img_id)

            if img_path is None or not os.path.exists(gt_path):
                print(f"[SKIP] {img_id} 파일 없음")
                continue

            image_np = cv2.imread(img_path)
            image_np = cv2.cvtColor(image_np, cv2.COLOR_BGR2RGB)

            points   = CLICK_POINTS.get(img_id, [[image_np.shape[1]//2, image_np.shape[0]//2]])
            result   = sam2.predict(image_np, points)
            sam_mask = result["mask"]

            gt_mask   = labelme_to_mask(gt_path, image_np.shape)
            coverage  = calc_coverage(sam_mask, gt_mask)
            precision = calc_precision(sam_mask, gt_mask)
            success   = coverage >= THRESHOLD

            vis = image_np.copy()
            vis[sam_mask > 127] = (vis[sam_mask > 127] * 0.5 + np.array([255, 0, 0]) * 0.5).astype(np.uint8)
            cv2.imwrite(f"eval_data/vis/{img_id}_result.jpg", cv2.cvtColor(vis, cv2.COLOR_RGB2BGR))

            results.append({
                "가구유형":   kor,
                "이미지":     img_id,
                "Coverage":  round(coverage, 4),
                "Precision": round(precision, 4),
                "성공여부":   "O" if success else "X"
            })
            print(f"[{img_id}] Coverage={coverage:.3f} Precision={precision:.3f} {'✓' if success else '✗'}")

    if not results:
        print("결과 없음!")
        return

    df = pd.DataFrame(results)
    df.to_csv("eval_results.csv", index=False)

    summary = df.groupby("가구유형").apply(lambda x: pd.Series({
        "테스트수":      len(x),
        "성공수":        (x["성공여부"] == "O").sum(),
        "평균Coverage":  f"{x['Coverage'].mean()*100:.1f}%",
        "평균Precision": f"{x['Precision'].mean()*100:.1f}%",
        "성공률":        f"{(x['성공여부']=='O').mean()*100:.1f}%"
    })).reset_index()

    print("\n=== 논문 표 ===")
    print(summary.to_string(index=False))
    summary.to_csv("eval_summary.csv", index=False, encoding="utf-8-sig")

if __name__ == "__main__":
    main()