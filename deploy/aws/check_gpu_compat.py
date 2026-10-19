#!/usr/bin/env python3
"""
이 호스트의 GPU 에서 프로젝트 모델이 쓰는 **CUDA 커널이 실제로 도는지** 작은 텐서로 확인한다.

왜 있나: 인스턴스 타입을 바꾸면 GPU 세대가 바뀐다(L4 sm_89 → T4 sm_75). PyTorch 휠은 sm_75 를
포함하지만, omni3d 환경의 detectron2 / pytorch3d `_C` 는 **개발 PC(RTX 4090, sm_89)에서 직접 빌드한**
확장이라 sm_75 커널이 들어 있는지 파일만 봐서는 확정할 수 없다(cuobjdump 도 없다). 들어 있지 않으면
"no kernel image is available" 로 **첫 요청에서** 죽는다. 그래서 실제로 불러 본다.

환경마다 그 환경의 python 으로 돌린다(smoke-test.sh 가 한다):
  $EMR_ROOT/miniconda3/envs/sam3d/bin/python   check_gpu_compat.py --env sam3d    # SAM2 / LaMa / SD
  $EMR_ROOT/miniconda3/envs/omni3d/bin/python  check_gpu_compat.py --env omni3d   # Omni3D
  $EMR_ROOT/miniconda3/envs/uLayout/bin/python check_gpu_compat.py --env uLayout  # uLayout

한계: 이 검사가 통과해도 **모델 전체가 그 GPU 에서 정상**이라는 뜻은 아니다. 커널이 로드되고 작은 입력에서
계산되는지만 본다. 전체 파이프라인은 smoke-test 의 실제 작업으로 확인한다.
통과/실패 한 줄씩 찍고, 하나라도 실패하면 종료코드 1.
"""
import argparse
import sys
import traceback


def probes_common(torch):
    dev = "cuda"

    def matmul_fp16():
        a = torch.randn(64, 64, device=dev, dtype=torch.float16)
        return float((a @ a).float().sum().item())

    def conv_fp16():
        x = torch.randn(1, 3, 32, 32, device=dev, dtype=torch.float16)
        w = torch.randn(4, 3, 3, 3, device=dev, dtype=torch.float16)
        return float(torch.nn.functional.conv2d(x, w).float().sum().item())

    def fft_fp32():  # LaMa 가 쓰는 cuFFT
        x = torch.randn(1, 4, 32, 32, device=dev)
        return float(torch.fft.rfftn(x, dim=(-2, -1)).abs().sum().item())

    def sdpa_fp16():  # SD / SAM2 어텐션
        q = torch.randn(1, 4, 16, 32, device=dev, dtype=torch.float16)
        return float(torch.nn.functional.scaled_dot_product_attention(q, q, q).float().sum().item())

    return [("fp16 matmul", matmul_fp16), ("fp16 conv2d", conv_fp16),
            ("cuFFT rfftn (LaMa)", fft_fp32), ("fp16 SDPA", sdpa_fp16)]


def probes_env(env, torch):
    dev = "cuda"
    p = []
    if env == "sam3d":
        def xformers_attn():
            import xformers.ops as xops
            q = torch.randn(1, 16, 4, 32, device=dev, dtype=torch.float16)
            return float(xops.memory_efficient_attention(q, q, q).float().sum().item())
        p.append(("xformers memory_efficient_attention", xformers_attn))

        def tv_nms():
            import torchvision
            b = torch.tensor([[0, 0, 10, 10], [1, 1, 11, 11.]], device=dev)
            return int(torchvision.ops.nms(b, torch.tensor([0.9, 0.8], device=dev), 0.5).numel())
        p.append(("torchvision nms", tv_nms))
    elif env == "omni3d":
        def d2_nms_rotated():
            from detectron2.layers import nms_rotated   # detectron2._C (직접 빌드)
            b = torch.tensor([[5, 5, 4, 4, 0], [5.5, 5.5, 4, 4, 0.]], device=dev)
            return int(nms_rotated(b, torch.tensor([0.9, 0.8], device=dev), 0.5).numel())

        def d2_roi_align():
            from detectron2.layers import ROIAlign
            x = torch.randn(1, 4, 16, 16, device=dev)
            r = torch.tensor([[0, 1, 1, 9, 9.]], device=dev)
            return float(ROIAlign((4, 4), 1.0, 0, aligned=True)(x, r).sum().item())

        def p3d_knn():
            from pytorch3d.ops import knn_points         # pytorch3d._C (직접 빌드)
            a = torch.randn(1, 32, 3, device=dev)
            return float(knn_points(a, a, K=2).dists.sum().item())

        def p3d_ball_query():
            from pytorch3d.ops import ball_query
            a = torch.randn(1, 32, 3, device=dev)
            return int(ball_query(a, a, K=4, radius=1.0).idx.numel())
        p += [("detectron2 nms_rotated (_C)", d2_nms_rotated), ("detectron2 ROIAlign (_C)", d2_roi_align),
              ("pytorch3d knn_points (_C)", p3d_knn), ("pytorch3d ball_query (_C)", p3d_ball_query)]
    elif env == "uLayout":
        def tv_nms():
            import torchvision
            b = torch.tensor([[0, 0, 10, 10], [1, 1, 11, 11.]], device=dev)
            return int(torchvision.ops.nms(b, torch.tensor([0.9, 0.8], device=dev), 0.5).numel())
        p.append(("torchvision nms", tv_nms))
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", required=True, choices=["sam3d", "omni3d", "uLayout"])
    a = ap.parse_args(argv)

    import torch
    if not torch.cuda.is_available():
        print(f"[{a.env}] ✗ CUDA 를 쓸 수 없다(torch {torch.__version__}, cuda {torch.version.cuda})")
        return 1
    cc = torch.cuda.get_device_capability(0)
    print(f"[{a.env}] GPU={torch.cuda.get_device_name(0)} sm_{cc[0]}{cc[1]} "
          f"torch={torch.__version__} cuda={torch.version.cuda} "
          f"wheel_archs={','.join(x.replace('sm_', '') for x in torch.cuda.get_arch_list())}")

    failed = 0
    for name, fn in probes_common(torch) + probes_env(a.env, torch):
        try:
            fn()
            torch.cuda.synchronize()
            print(f"  ✔ {name}")
        except Exception as e:  # 커널 이미지 없음, import 실패 등 전부 실패로 센다
            failed += 1
            msg = str(e).strip().splitlines()[0][:140] if str(e).strip() else type(e).__name__
            print(f"  ✗ {name}: {type(e).__name__}: {msg}")
            if "no kernel image" in str(e):
                print(f"    → 이 GPU(sm_{cc[0]}{cc[1]})용 커널이 확장에 없다. 해당 환경을 이 아키텍처로 다시 빌드해야 한다.")
    print(f"[{a.env}] {'전부 통과' if not failed else f'실패 {failed}건'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
