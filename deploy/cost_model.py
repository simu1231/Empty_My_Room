#!/usr/bin/env python3
"""스케일투제로 GPU 워커의 월 비용 추정 — 구성 ①/② 비교.

왜 스크립트로 남기는가: 이 비교의 답은 "어느 쪽이 싸다"가 아니라 **트래픽에
따라 답이 뒤집힌다**는 것이다. 가정을 바꿔 다시 돌려볼 수 있어야 의미가 있다.

핵심 직관 하나만 기억하면 된다.
  저트래픽 구간에서 비용을 지배하는 건 '작업 시간'이 아니라 '유휴 타임아웃'이다.
  50초짜리 작업 하나를 위해 부팅 + 작업 + IDLE_EXIT_SEC 만큼 인스턴스를 켜 둔다.
  IDLE_EXIT_SEC를 줄이면 요금이 바로 줄지만, 대신 콜드스타트를 더 자주 낸다.

가격 출처: DoiT Compute, ap-northeast-2(서울), 2026-09 조회. 스팟가는 변동하므로
배포 직전에 describe-spot-price-history로 반드시 다시 확인할 것.
"""
from dataclasses import dataclass

# ── 가격 (ap-northeast-2, USD/hr) ────────────────────────────────────
# 온디맨드는 AWS 공식 가격표(pricing.us-east-1.amazonaws.com, ap-northeast-2,
# 2026-09-30 조회)에서 그대로 가져온 값이다. 스팟은 공개 가격표에 없어서
# describe-spot-price-history 없이는 확정할 수 없다 — g5/g4dn은 DoiT 실측값을
# 쓰고, g6/g6e는 g5의 스팟 할인율(45.9%)을 적용한 **추정치**다(아래 SPOT_EST).
PRICES = {
    #                 온디맨드   스팟      VRAM(MiB)  비고
    "g5.xlarge":   (1.2370, 0.5674, 23028),  # A10G — 24GB가 아니라 23028 MiB다
    "g6.xlarge":   (0.9896, 0.4543, 23034),  # L4  — 같은 24GB급인데 g5보다 20% 싸다
    "g6e.xlarge":  (2.2880, 1.0502, 46068),  # L40S 48GB — 세 컨테이너가 다 들어간다
    "g4dn.xlarge": (0.6470, 0.2740, 15360),  # T4  — 스팟은 0.22~0.33 범위의 중앙값
}
# 스팟 가격을 실측하지 않은 인스턴스(추정치를 쓴 것) — 보고서에 표시하려고 둔다
SPOT_EST = {"g6.xlarge", "g6e.xlarge"}

SPOT_FALLBACK_RATE = 0.10   # 스팟 용량을 못 잡아 온디맨드로 떨어지는 비율(중단율 5~10%)

# 상시 비용 (두 구성에 공통이라 비교에는 영향 없지만, 총액 감각을 위해 포함)
# AMI 크기는 실측이다. 컨테이너가 자기완결적이 아니라서(conda 환경과 소스를
# 호스트에서 bind-mount) AMI에 다음을 다 구워 넣어야 한다:
#   conda 환경 sam3d 12G + uLayout 6.9G + omni3d 13G = 31.9G
#   소스 sam-3d-objects 2.3G + uLayout 1.2G + omni3d/detectron2/pytorch3d 0.5G = 4.0G
#   도커 이미지 3.0G + OS/드라이버 약 8G
AMI_GB = 47

ALWAYS_ON = {
    "API 서버 (t4g.small)":      0.0208 * 730,   # AWS 공식 가격표 실측
    f"AMI 스냅샷 {AMI_GB}GB":     AMI_GB * 0.05,
    "S3 + DynamoDB + SQS":       2.0,
}


@dataclass
class Workload:
    """한 '세션' = 사용자가 앱을 열고 방 하나를 처리하는 단위."""
    sessions_per_day: int
    sam3d_jobs: int = 3        # 가구 3개
    scene_jobs: int = 4        # 방 치수 1 + 가구 크기 3
    sam3d_sec: float = 30      # 웜 기준 실측 25~30초
    scene_sec: float = 2       # 실측 1~3초
    sam3d_load_sec: float = 24 # 파이프라인 로드 실측
    boot_sec: float = 180      # EC2 기동 + 컨테이너 기동
    # 주의: 스냅샷에서 복원한 EBS 볼륨은 블록을 **처음 읽을 때** S3에서 지연
    # 로딩된다(lazy load). conda 환경 30GB 중 실제로 읽는 10GB 남짓을 실효
    # 50~100MB/s로 당겨오면 첫 부팅에만 2분 이상이 더 붙는다. Fast Snapshot
    # Restore는 이걸 없애주지만 스냅샷·AZ당 시간 $0.75(월 $540)라 스케일투제로
    # 취지에 정면으로 어긋난다. boot_sensitivity()로 영향 범위를 본다.
    idle_sam3d: float = 900    # IDLE_EXIT_SEC 현재 기본값 15분
    idle_scene: float = 900


# 루트 gp3 볼륨은 인스턴스가 살아 있는 동안만 존재한다(종료 시 삭제).
# GB-월 $0.092를 시간당으로 환산해 인스턴스 단가에 얹는다.
EBS_HOURLY = AMI_GB * 0.092 / 730


def hourly(instance, spot=True):
    od, sp, _ = PRICES[instance]
    rate = od if not spot else sp * (1 - SPOT_FALLBACK_RATE) + od * SPOT_FALLBACK_RATE
    return rate + EBS_HOURLY


def option1(w: Workload, spot=True, inst="g5.xlarge"):
    """① 한 인스턴스에 셋 다.

    A10G 용량으로 좁힌 카드에서 8회 반복 부하로 실제 통과를 확인했다
    (deploy/simulate_a10g.py + 종단 테스트). 캐싱 할당자가 남은 공간에 맞춰
    캐시를 줄이기 때문에, 단독 측정치(22.3GB)를 그냥 더해서 판단하면 안 된다.
    """
    work = w.sam3d_load_sec + w.sam3d_jobs * w.sam3d_sec + w.scene_jobs * w.scene_sec
    per_session = w.boot_sec + work + w.idle_sam3d
    hours = per_session / 3600 * w.sessions_per_day * 30
    return {inst: hours * hourly(inst, spot)}, {inst: hours}


def option3(w: Workload, spot=True):
    """③ 더 큰 카드 한 대(g6e.xlarge, L40S 48GB).

    ①과 구조는 같고 카드만 키운 것이다. 시간당 단가는 g5의 1.85배지만
    유휴 타임아웃은 여전히 **한 번만** 낸다 — ②와 비교할 때 이 점이 핵심이다.
    VRAM이 두 배라 캐시를 쥐어짤 필요가 없어 OOM 여유가 가장 크다.
    """
    return option1(w, spot, inst="g6e.xlarge")


def option2(w: Workload, spot=True):
    """② sam3d(g5)와 scene(g4dn) 분리 — 두 인스턴스가 각자 유휴 타임아웃을 낸다."""
    sam_work = w.sam3d_load_sec + w.sam3d_jobs * w.sam3d_sec
    sam_h = (w.boot_sec + sam_work + w.idle_sam3d) / 3600 * w.sessions_per_day * 30
    # scene은 모델 로드가 빨라 콜드스타트가 싸다 → 유휴 타임아웃을 짧게 가져갈 수 있다
    scn_work = w.scene_jobs * w.scene_sec
    scn_h = (w.boot_sec + scn_work + w.idle_scene) / 3600 * w.sessions_per_day * 30
    return ({"g5.xlarge": sam_h * hourly("g5.xlarge", spot),
             "g4dn.xlarge": scn_h * hourly("g4dn.xlarge", spot)},
            {"g5.xlarge": sam_h, "g4dn.xlarge": scn_h})


def report(sessions_list=(5, 20, 50, 200), spot=True, idle=None):
    base = sum(ALWAYS_ON.values())
    mode = "스팟 우선(온디맨드 폴백 10%)" if spot else "온디맨드"
    idle_note = f", IDLE_EXIT_SEC={idle}초" if idle else ", IDLE_EXIT_SEC=기본 900초"
    print(f"\n{'='*88}\n구성별 월 예상 비용 — {mode}, ap-northeast-2{idle_note}")
    print(f"상시 비용(공통) ${base:.2f}/월 = " +
          ", ".join(f"{k} ${v:.2f}" for k, v in ALWAYS_ON.items()))
    print("="*88)
    print(f"{'세션/일':>7} {'①g5 한대':>11} {'①g6 한대':>11} {'②분리':>11} "
          f"{'③g6e 한대':>11} {'가장 싼 구성':>14}")
    print("-"*88)
    for n in sessions_list:
        kw = dict(sessions_per_day=n)
        if idle:
            kw.update(idle_sam3d=idle, idle_scene=idle)
        w = Workload(**kw)
        vals = {
            "①g5":  sum(option1(w, spot)[0].values()) + base,
            "①g6":  sum(option1(w, spot, "g6.xlarge")[0].values()) + base,
            "②":    sum(option2(w, spot)[0].values()) + base,
            "③g6e": sum(option3(w, spot)[0].values()) + base,
        }
        best = min(vals, key=vals.get)
        print(f"{n:>7} {vals['①g5']:>10.0f}$ {vals['①g6']:>10.0f}$ {vals['②']:>10.0f}$ "
              f"{vals['③g6e']:>10.0f}$ {best+' ('+format(vals[best],'.0f')+'$)':>14}")
    print("="*88)
    if SPOT_EST and spot:
        print(f"※ 스팟 추정치를 쓴 인스턴스: {', '.join(sorted(SPOT_EST))} "
              f"(g5 할인율 45.9% 적용 — 배포 전 describe-spot-price-history로 확인할 것)")


def boot_sensitivity(sessions=20, spot=True, inst="g6.xlarge", idle=120):
    """부팅 시간이 길어질 때 비용이 얼마나 늘어나는지.

    스냅샷 지연 로딩 때문에 부팅이 예상보다 길어질 수 있어서, 이 변수에
    비용이 얼마나 민감한지 미리 본다.
    """
    base = sum(ALWAYS_ON.values())
    print(f"\n부팅 시간 민감도 (세션 {sessions}/일, {inst}, IDLE {idle}초, 스팟)")
    print(f"{'부팅':>8} {'월 비용':>10} {'부팅이 차지하는 비중':>20}")
    print("-"*42)
    for boot in (60, 180, 300, 420, 600):
        w = Workload(sessions_per_day=sessions, boot_sec=boot,
                     idle_sam3d=idle, idle_scene=idle)
        c, h = option1(w, spot, inst)
        total = sum(c.values()) + base
        per = w.boot_sec + (w.sam3d_load_sec + w.sam3d_jobs*w.sam3d_sec
                            + w.scene_jobs*w.scene_sec) + w.idle_sam3d
        print(f"{boot:>6}초 {total:>9.0f}$ {boot/per*100:>19.0f}%")


def idle_sensitivity(sessions=20, spot=True):
    """유휴 타임아웃이 비용을 얼마나 지배하는지 — 이 표가 이 문서의 핵심이다."""
    base = sum(ALWAYS_ON.values())
    print(f"\n유휴 타임아웃 민감도 (세션 {sessions}/일, 구성 ①)")
    print(f"{'IDLE_EXIT':>10} {'월 비용':>10} {'유휴가 차지하는 비중':>22}")
    print("-"*46)
    for idle in (60, 120, 300, 600, 900, 1800):
        w = Workload(sessions_per_day=sessions, idle_sam3d=idle, idle_scene=idle)
        c, h = option1(w, spot)
        total = sum(c.values())
        work_only = Workload(sessions_per_day=sessions, idle_sam3d=0, idle_scene=0)
        cw, _ = option1(work_only, spot)
        share = (total - sum(cw.values())) / total * 100 if total else 0
        print(f"{idle:>8}초 {total+base:>9.0f}$ {share:>21.0f}%")


if __name__ == "__main__":
    report()
    report(spot=False)
    idle_sensitivity()
