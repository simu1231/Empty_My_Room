#!/usr/bin/env python3
"""
SD 인페인팅 + canny ControlNet 가중치를 fp16 safetensors 로 1회 변환한다.

■ 왜 필요한가
GPU 워커가 새로 뜬 뒤 첫 인페인팅에서 SD 로드가 **553초** 걸린다(CloudWatch
실측 553.15초 / 532.53초, 서로 다른 인스턴스의 첫 호출). 추론 자체는 12~15초라
로드가 40배다. 원인을 캐시 실물에서 확인했다.

    models--runwayml--stable-diffusion-inpainting
        3.3G  unet/diffusion_pytorch_model.bin        ← fp32 pickle
        470M  text_encoder/pytorch_model.bin          ← fp32 pickle
        320M  vae/diffusion_pytorch_model.bin         ← fp32 pickle
    fp16 변형 0개 / safetensors 0개

sd_service.py 는 `torch_dtype=torch.float16` 만 주고 fp16 변형을 요구하지
않는다. 그래서 **fp32 를 통째로 읽어 메모리에서 캐스팅**한다. 게다가 `.bin` 은
pickle 이라 `torch.load` 가 단일 스레드로 역직렬화한다 — mmap 이 안 된다.

이 스크립트는 두 가지를 동시에 없앤다.
    읽을 바이트   5.4GB → 약 2.8GB   (fp32 → fp16)
    역직렬화 방식 pickle → mmap       (.bin → .safetensors)

■ 왜 빌더가 아니라 개발 PC 에서 돌리는가
pack-envs.sh 가 hfcache.tar 를 **로컬 ~/.cache/huggingface 에서** 만들어 S3 에
올리고, bootstrap-ami.sh 가 빌더에서 그걸 풀어 쓴다. 즉 변환 결과가 로컬 캐시
안에 있어야 AMI 까지 자동으로 흘러간다. 빌더에서 변환하면 AMI 를 구울 때마다
매번 다시 해야 한다.

■ 왜 variant="fp16" 을 쓰지 않는가
`variant` 는 한 저장소 안에 fp32 와 fp16 가 **함께** 있을 때 고르기 위한
장치다. 여기 출력 디렉터리에는 fp16 만 있다. variant 를 붙이면 파일명이
`*.fp16.safetensors` 가 되고, 읽는 쪽이 variant 를 빠뜨리는 순간 "파일이
없다" 가 아니라 **조용히 다른 걸 찾는** 실패가 생긴다. 쓰는 쪽과 읽는 쪽이
반드시 짝이 맞아야 하는 인자를 하나 줄인다.

■ 사용법
    conda run -n sam3d python tools/convert_sd_fp16.py
    conda run -n sam3d python tools/convert_sd_fp16.py --no-smoke   # GPU 검증 생략

멱등하다. 이미 변환돼 있으면 건너뛴다(--force 로 덮어쓰기).
"""
import argparse
import os
import shutil
import sys
import time
from pathlib import Path

# 네트워크를 쓰지 않는다. 캐시가 불완전하면 조용히 받아오는 대신 여기서
# 실패해야 한다 — 그래야 "AMI 에는 들어갔는데 인스턴스에서 받으려다 터지는"
# 경우가 안 생긴다.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

SD_REPO = "runwayml/stable-diffusion-inpainting"
CN_REPO = "lllyasviel/control_v11p_sd15_canny"

HF_HOME = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
# 출력은 HF 캐시 **안**에 둔다. pack-envs.sh 가 tar 로 묶는 범위가
# `-C ~/.cache huggingface` 라서, 이 밖에 두면 AMI 로 따라가지 않는다.
OUT_ROOT = HF_HOME / "emr-sd-fp16"
OUT_SD = OUT_ROOT / "sd-inpainting"
OUT_CN = OUT_ROOT / "controlnet-canny"


def tree_bytes(path: Path) -> int:
    """심링크를 따라간 실제 바이트. HF 캐시는 blobs/ 를 심링크로 가리킨다."""
    total, seen = 0, set()
    for p in path.rglob("*"):
        try:
            real = p.resolve()
            if not real.is_file():
                continue
            key = (real.stat().st_dev, real.stat().st_ino)
            if key in seen:          # 하드링크 중복 계산 방지
                continue
            seen.add(key)
            total += real.stat().st_size
        except OSError:
            continue
    return total


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0


def cache_dir_for(repo: str) -> Path:
    return HF_HOME / "hub" / ("models--" + repo.replace("/", "--"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="이미 있어도 다시 변환")
    ap.add_argument("--no-smoke", action="store_true", help="GPU 추론 검증 생략")
    args = ap.parse_args()

    import torch
    from diffusers import (ControlNetModel, DDIMScheduler,
                           StableDiffusionControlNetInpaintPipeline)

    print("▶ 변환 전 캐시 실측")
    before = 0
    for repo in (SD_REPO, CN_REPO):
        d = cache_dir_for(repo)
        if not d.is_dir():
            print(f"✗ 캐시에 {repo} 가 없습니다: {d}")
            print("  이 스크립트는 네트워크를 쓰지 않습니다(HF_HUB_OFFLINE=1).")
            return 1
        b = tree_bytes(d)
        before += b
        print(f"    {repo:48s} {human(b):>9s}")
    print(f"    {'합계':48s} {human(before):>9s}")

    if OUT_ROOT.exists() and not args.force:
        after = tree_bytes(OUT_ROOT)
        print(f"\n▶ 이미 변환돼 있습니다: {OUT_ROOT}  ({human(after)})")
        print("  다시 하려면 --force")
        return 0
    if OUT_ROOT.exists():
        print(f"\n▶ --force: 기존 출력 삭제 {OUT_ROOT}")
        shutil.rmtree(OUT_ROOT)

    # ── 로드 ────────────────────────────────────────────────────────────
    # CPU 로 로드한다. GPU 는 필요 없고, 24GB VRAM 을 fp32 중간 표현으로 채울
    # 이유도 없다. 이 로드가 느린 것이 바로 우리가 없애려는 그 현상이다.
    print("\n▶ fp32 로드 (이게 느린 게 정상 — 지금 고치려는 현상입니다)")
    t0 = time.time()
    controlnet = ControlNetModel.from_pretrained(CN_REPO, torch_dtype=torch.float16)
    t_cn = time.time() - t0
    print(f"    ControlNet  {t_cn:7.2f}초")

    t0 = time.time()
    pipe = StableDiffusionControlNetInpaintPipeline.from_pretrained(
        SD_REPO, controlnet=controlnet,
        torch_dtype=torch.float16, safety_checker=None)
    t_sd = time.time() - t0
    print(f"    파이프라인  {t_sd:7.2f}초")
    print(f"    로드 합계   {t_cn + t_sd:7.2f}초")

    # sd_service.py 가 런타임에 하는 스케줄러 교체를 저장 시점에 미리 굳힌다.
    # 런타임에서 from_config 를 또 불러도 결과는 같지만, 저장된 설정과 실제
    # 동작이 다르면 나중에 이 디렉터리를 들여다보는 사람이 헷갈린다.
    pipe.scheduler = DDIMScheduler.from_config(pipe.scheduler.config)

    # ── 저장 ────────────────────────────────────────────────────────────
    print(f"\n▶ fp16 safetensors 저장 → {OUT_ROOT}")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    # ControlNet 은 파이프라인과 **따로** 저장한다. sd_service.py 가 지금도
    # ControlNetModel 을 먼저 만들어 파이프라인에 넘기는 구조이고, 그 구조를
    # 바꾸지 않는 쪽이 변경 면적이 작다.
    t0 = time.time()
    controlnet.save_pretrained(OUT_CN, safe_serialization=True)
    print(f"    ControlNet  {time.time() - t0:7.2f}초")

    # 파이프라인에 들린 controlnet 을 빼고 저장한다. 안 빼면 같은 1.4GB 가
    # 파이프라인 안에 한 번 더 들어가 디스크와 AMI 스냅샷을 두 배로 먹는다.
    t0 = time.time()
    pipe.controlnet = None
    pipe.save_pretrained(OUT_SD, safe_serialization=True)
    print(f"    파이프라인  {time.time() - t0:7.2f}초")

    # ── 결과 ────────────────────────────────────────────────────────────
    after = tree_bytes(OUT_ROOT)
    print(f"\n▶ 결과")
    print(f"    변환 전   {human(before):>9s}")
    print(f"    변환 후   {human(after):>9s}   ({after / before * 100:.1f}%)")
    print(f"    절감      {human(before - after):>9s}")
    print("\n  파일별:")
    for p in sorted(OUT_ROOT.rglob("*.safetensors")):
        print(f"    {human(p.stat().st_size):>9s}  {p.relative_to(OUT_ROOT)}")

    leftover = list(OUT_ROOT.rglob("*.bin"))
    if leftover:
        print("\n✗ .bin 이 남았습니다 — safe_serialization 이 안 먹은 컴포넌트가 있습니다:")
        for p in leftover:
            print(f"    {human(p.stat().st_size):>9s}  {p.relative_to(OUT_ROOT)}")
        return 1

    # ── 검증 ────────────────────────────────────────────────────────────
    # "저장은 됐다" 와 "다시 읽어서 그림이 나온다" 는 다르다. 여기서 안 보면
    # AMI 를 굽고 GPU 를 띄운 뒤에 알게 된다.
    del pipe, controlnet
    import gc
    gc.collect()

    print("\n▶ 되읽기 검증 (sd_service.py 가 할 그대로)")
    t0 = time.time()
    cn2 = ControlNetModel.from_pretrained(OUT_CN, torch_dtype=torch.float16)
    pipe2 = StableDiffusionControlNetInpaintPipeline.from_pretrained(
        OUT_SD, controlnet=cn2, torch_dtype=torch.float16, safety_checker=None)
    t_reload = time.time() - t0
    print(f"    되읽기      {t_reload:7.2f}초   (fp32 로드는 {t_cn + t_sd:.2f}초였다)")

    for name in ("unet", "vae", "text_encoder", "controlnet"):
        comp = getattr(pipe2, name, None)
        if comp is None:
            print(f"✗ 컴포넌트 누락: {name}")
            return 1
        dt = next(comp.parameters()).dtype
        if dt != torch.float16:
            print(f"✗ {name} dtype 이 {dt} 입니다 (fp16 이어야 함)")
            return 1
        print(f"    {name:12s} fp16 ✓")

    if args.no_smoke:
        print("\n▶ GPU 추론 검증 생략(--no-smoke)")
    elif not torch.cuda.is_available():
        print("\n▶ GPU 가 없어 추론 검증을 생략합니다")
    else:
        print("\n▶ GPU 추론 검증 (2스텝 64x64 — 그림 품질이 아니라 '돌아가는지'만 본다)")
        from PIL import Image
        pipe2 = pipe2.to("cuda")
        pipe2.enable_attention_slicing()
        img = Image.new("RGB", (64, 64), (128, 128, 128))
        mask = Image.new("L", (64, 64), 0)
        mask.paste(255, (16, 16, 48, 48))
        canny = Image.new("RGB", (64, 64), (0, 0, 0))
        t0 = time.time()
        out = pipe2(prompt="empty room interior", image=img, mask_image=mask,
                    control_image=canny, num_inference_steps=2,
                    generator=torch.Generator("cuda").manual_seed(42)).images[0]
        print(f"    추론 {time.time() - t0:.2f}초, 출력 {out.size} ✓")

    print(f"\n✔ 완료: {OUT_ROOT}")
    print(f"  EMR_SD_PATH 로 넘길 값(인스턴스 기준): "
          f"$EMR_ROOT/.cache/huggingface/emr-sd-fp16")
    return 0


if __name__ == "__main__":
    sys.exit(main())
