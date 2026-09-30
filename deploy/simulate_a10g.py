#!/usr/bin/env python3
"""g5.xlarge(A10G)를 로컬 RTX 4090 위에서 흉내 내는 용량 테스트.

왜 필요한가: A10G는 흔히 "24GB"로 소개되지만 nvidia-smi가 보고하는 총량은
**23,028 MiB**로, 개발에 쓰는 RTX 4090(24,564 MiB)보다 1,536 MiB 적다.
로컬에서 아슬아슬하게 돌던 구성이 g5에서는 OOM으로 죽을 수 있다는 뜻이다.

방법: **컨테이너가 보게 될 여유 VRAM을 g5와 똑같이 맞춘다.** 단순히 차이만큼
(1,536 MiB) 점유하면 안 된다. 로컬에는 Xwayland 등 데스크톱이 700MiB쯤 이미
쓰고 있고 이 스크립트 자신도 CUDA 컨텍스트로 400MiB쯤 더 먹기 때문에, 그렇게
하면 g5보다 1,600 MiB 더 빡빡한 조건이 된다. 그러면 "g5에서 안 된다"가 아니라
"내가 만든 조건에서 안 된다"를 측정하게 된다. 그래서 자기 오버헤드를 실측하고
역산해서 점유량을 정한다.

    python deploy/simulate_a10g.py            # 점유하고 대기 (Ctrl+C로 해제)
    python deploy/simulate_a10g.py --check    # 점유해야 할 용량만 출력
"""
import argparse, subprocess, sys, time

A10G_TOTAL_MIB = 23028      # g5.xlarge의 nvidia-smi 보고값
# 헤드리스 EC2에는 데스크톱이 없어서 카드가 거의 비어 있다(nvidia-smi 4 MiB 내외).
TARGET_RESIDUAL_MIB = 4


def gpu_total_used():
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.total,memory.used",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=15).stdout.strip().splitlines()[0]
    total, used = (int(x) for x in out.split(","))
    return total, used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="점유량만 계산하고 끝낸다")
    ap.add_argument("--target", type=int, default=A10G_TOTAL_MIB,
                    help="흉내 낼 카드의 총 VRAM(MiB)")
    a = ap.parse_args()

    total, used = gpu_total_used()
    # 목표는 "카드를 몇 MiB로 만든다"가 아니라 "여유를 g5와 같게 만든다"이다.
    target_free = a.target - TARGET_RESIDUAL_MIB
    free_now = total - used
    print(f"로컬 카드 총량 {total} MiB / 현재 사용 {used} MiB → 여유 {free_now} MiB")
    print(f"흉내 낼 카드   {a.target} MiB (헤드리스 잔량 {TARGET_RESIDUAL_MIB} MiB) "
          f"→ 목표 여유 {target_free} MiB")
    if free_now <= target_free:
        print(f"이미 여유가 목표보다 적다({free_now} <= {target_free}). 점유할 것이 없다 "
              f"— 오히려 {target_free - free_now} MiB 부족한, g5보다 빡빡한 조건이다.")
        return 0
    if a.check:
        print(f"점유해야 할 용량: {free_now - target_free} MiB")
        return 0

    import torch
    if not torch.cuda.is_available():
        print("CUDA를 쓸 수 없다."); return 1

    # 1) 컨텍스트만 먼저 만들어 이 프로세스 자신의 오버헤드를 실측한다.
    torch.zeros(1, device="cuda"); torch.cuda.synchronize()
    _, used_ctx = gpu_total_used()
    print(f"이 스크립트 자신의 CUDA 컨텍스트: {used_ctx - used} MiB (역산에 반영)")

    # 2) 남은 여유가 정확히 target_free 가 되도록 블록 크기를 정한다.
    block_mib = (total - used_ctx) - target_free
    block = None
    if block_mib <= 0:
        print("컨텍스트만으로 이미 목표를 넘겼다 — 추가 블록 없이 진행한다.")
    else:
        # 캐싱 할당자가 되돌려주지 않도록 그냥 붙잡고 있는다
        block = torch.empty(block_mib * 1024 * 1024, dtype=torch.uint8, device="cuda")
        torch.cuda.synchronize()

    _, used_after = gpu_total_used()
    actual_free = total - used_after
    print(f"\n점유 완료. 현재 사용 {used_after} MiB → 남은 여유 {actual_free} MiB "
          f"(목표 {target_free}, 오차 {actual_free - target_free:+d} MiB)")
    print("이 상태로 평소처럼 작업을 돌려보라. Ctrl+C로 해제한다.", flush=True)
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n해제.")
    del block
    return 0


if __name__ == "__main__":
    sys.exit(main())
