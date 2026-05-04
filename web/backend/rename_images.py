import os, shutil

SOURCE_DIR = "/mnt/c/Users/labpc/Desktop/"
TARGET_DIR = "eval_data/images/"

furniture_files = {
    "bed":   ["침대1.png", "침대2.jpg", "침대3.jpg", "침대4.jpg"],
    "chair": ["의자1.png", "의자2.png", "의자3.png", "의자4.png"],
    "desk":  ["책상1.jpg", "책상2.jpg", "책상3.jpg", "책상4.jpg"],
}

os.makedirs(TARGET_DIR, exist_ok=True)

for eng, files in furniture_files.items():
    for i, fname in enumerate(files, 1):
        src = os.path.join(SOURCE_DIR, fname)
        # 확장자 그대로 유지
        ext = os.path.splitext(fname)[1]
        dst = os.path.join(TARGET_DIR, f"{eng}_{i:02d}{ext}")
        if os.path.exists(src):
            shutil.copy(src, dst)
            print(f"✓ {fname} → {eng}_{i:02d}{ext}")
        else:
            print(f"✗ 파일 없음: {fname}")