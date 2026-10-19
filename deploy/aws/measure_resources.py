#!/usr/bin/env python3
"""
GPU 호스트의 **시스템 RAM · VRAM · 메모리 압력 · 디스크 읽기**를 시간순으로 재고 요약한다.

왜 있나: SAM3D 를 fal 로 돌리면 VRAM 은 약 19GB 줄지만 Host RAM 이 충분한지는 별개다.
SAM3D 를 뺀 구성의 RAM 은 저장소에 측정값이 없고, RAM 이 모자라면 VRAM 이 남아도
스래싱(EBS 읽기 상한 고정, SSH banner 타임아웃으로 위장)이 난다 — 2026-10-07 에 겪은 일이다.
그래서 VRAM 만이 아니라 /proc/meminfo, PSI(/proc/pressure), 디스크 IOPS 를 같이 잰다.

표준 라이브러리만 쓴다(빌더/워커 호스트의 시스템 python3 로 돈다).

  python3 measure_resources.py sample --out DIR [--interval 2] [--docker-every 5]
      SIGTERM/SIGINT 까지 DIR/samples.csv 에 쌓는다.
  python3 measure_resources.py mark   --out DIR LABEL
      구간 표시(DIR/marks.txt). 요약이 구간별 최악값을 보여 준다.
  python3 measure_resources.py summarize --out DIR [기준 옵션] [--min-samples N]
      DIR/report.md, DIR/report.json 을 만들고 표준출력에도 찍는다.
      **종료 코드**: 0 = 모든 기준 통과 / 3 = 기준 미달 / 4 = 측정 결과가 없거나 부족(샘플 없음·너무 짧음).
      호출하는 쪽(smoke-test.sh)이 EMR_MEASURE_ENFORCE 로 이 코드를 실패로 볼지 정한다.

판정 기준(기본값)은 **초안**이다. 보정된 값이 아니다. 통과해도 "그 한 번의 측정 동안,
실행해 본 작업에서" 문제가 없었다는 뜻일 뿐 운영 인스턴스 타입의 확정 근거가 아니다.
"""
import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time

DISK_RE = re.compile(r"^(nvme\d+n\d+|xvd[a-z]+|sd[a-z]+|vd[a-z]+)$")
FIELDS = ["t", "mem_total_mib", "mem_avail_mib", "cached_mib", "swap_used_mib",
          "psi_mem_some10", "psi_mem_full10", "psi_io_some10", "psi_io_full10",
          "majfault_ps", "disk_read_iops", "gpu_used_mib", "gpu_total_mib", "gpu_util",
          "load1", "containers"]


# ── 읽기 ────────────────────────────────────────────────────────────────

def read_meminfo(path="/proc/meminfo"):
    d = {}
    with open(path) as f:
        for line in f:
            k, _, v = line.partition(":")
            d[k] = int(v.split()[0])  # kB
    return d


def read_psi(path):
    """{'some': avg10, 'full': avg10}. PSI 가 없는 커널이면 None."""
    try:
        out = {}
        with open(path) as f:
            for line in f:
                kind, *rest = line.split()
                kv = dict(x.split("=") for x in rest)
                out[kind] = float(kv["avg10"])
        return out
    except (OSError, KeyError, ValueError):
        return None


def read_vmstat_majfault(path="/proc/vmstat"):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("pgmajfault "):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def read_disk_reads(path="/proc/diskstats"):
    """파티션이 아닌 블록 장치의 읽기 완료 수 합."""
    total = 0
    try:
        with open(path) as f:
            for line in f:
                p = line.split()
                if len(p) > 3 and DISK_RE.match(p[2]):
                    total += int(p[3])
    except OSError:
        return None
    return total


def read_gpu():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True).stdout.strip().splitlines()[0]
        used, total, util = [float(x) for x in out.split(",")]
        return used, total, util
    except Exception:
        return None


def read_containers():
    """컨테이너별 메모리(MiB). docker stats 는 느려서(수 초) 간격을 두고 부른다."""
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
            capture_output=True, text=True, timeout=30, check=True).stdout
    except Exception:
        return None
    res = {}
    for line in out.splitlines():
        name, _, usage = line.partition("\t")
        m = re.match(r"([\d.]+)\s*([KMG]i?B)", usage.strip())
        if not m:
            continue
        v, unit = float(m.group(1)), m.group(2)
        mib = v * {"KiB": 1 / 1024, "KB": 1 / 1024, "MiB": 1, "MB": 1, "GiB": 1024, "GB": 1024}[unit]
        res[name] = round(mib, 1)
    return res


def instance_type():
    """IMDSv2 로 인스턴스 타입. 못 읽으면 'unknown'(로컬/컨테이너)."""
    import urllib.request
    try:
        req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        tok = urllib.request.urlopen(req, timeout=1).read().decode()
        req = urllib.request.Request("http://169.254.169.254/latest/meta-data/instance-type",
                                     headers={"X-aws-ec2-metadata-token": tok})
        return urllib.request.urlopen(req, timeout=1).read().decode()
    except Exception:
        return "unknown"


# ── sample ──────────────────────────────────────────────────────────────

def cmd_sample(a):
    os.makedirs(a.out, exist_ok=True)
    stop = {"v": False}

    def _stop(*_):
        stop["v"] = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    meta = {"instance_type": instance_type(), "label": a.label, "started": time.time(),
            "interval": a.interval}
    json.dump(meta, open(os.path.join(a.out, "meta.json"), "w"))

    path = os.path.join(a.out, "samples.csv")
    t0 = time.time()
    prev_t, prev_mf, prev_rd = t0, read_vmstat_majfault(), read_disk_reads()
    containers, i = "", 0
    with open(path, "w", newline="", buffering=1) as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        while True:   # 최소 한 번은 잰다(시작하자마자 멈춰도 요약이 빈 채로 나오지 않게)
            now = time.time()
            mi = read_meminfo()
            pm, pi = read_psi("/proc/pressure/memory"), read_psi("/proc/pressure/io")
            mf, rd = read_vmstat_majfault(), read_disk_reads()
            dt = max(now - prev_t, 1e-6)
            maj = (mf - prev_mf) / dt if mf is not None and prev_mf is not None else ""
            iops = (rd - prev_rd) / dt if rd is not None and prev_rd is not None else ""
            prev_t, prev_mf, prev_rd = now, mf, rd
            g = read_gpu()
            if a.docker_every and i % a.docker_every == 0:
                c = read_containers()
                containers = json.dumps(c, ensure_ascii=False) if c else ""
            else:
                containers = ""
            swap_used = (mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) / 1024
            w.writerow([
                round(now - t0, 1), round(mi["MemTotal"] / 1024), round(mi["MemAvailable"] / 1024),
                round(mi.get("Cached", 0) / 1024), round(swap_used),
                "" if not pm else pm.get("some", ""), "" if not pm else pm.get("full", ""),
                "" if not pi else pi.get("some", ""), "" if not pi else pi.get("full", ""),
                "" if maj == "" else round(maj, 1), "" if iops == "" else round(iops, 1),
                "" if not g else g[0], "" if not g else g[1], "" if not g else g[2],
                os.getloadavg()[0], containers])
            i += 1
            if stop["v"]:
                break
            # 인터벌을 지키되 SIGTERM 에 바로 반응한다
            end = now + a.interval
            while not stop["v"] and time.time() < end:
                time.sleep(0.2)
    print(f"[measure] 샘플 {i}개 → {path}")


def cmd_mark(a):
    os.makedirs(a.out, exist_ok=True)
    meta = os.path.join(a.out, "meta.json")
    t0 = json.load(open(meta))["started"] if os.path.exists(meta) else time.time()
    with open(os.path.join(a.out, "marks.txt"), "a") as f:
        f.write(f"{time.time() - t0:.1f}\t{a.label}\n")


# ── summarize ───────────────────────────────────────────────────────────

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _col(rows, k):
    return [x for x in (_f(r[k]) for r in rows) if x is not None]


def _peak(rows, k, fn):
    v = _col(rows, k)
    return fn(v) if v else None


def summarize_rows(rows, marks, a, meta):
    total = _peak(rows, "mem_total_mib", max)
    min_avail = _peak(rows, "mem_avail_mib", min)
    gpu_total = _peak(rows, "gpu_total_mib", max)
    gpu_peak = _peak(rows, "gpu_used_mib", max)
    swap_col = _col(rows, "swap_used_mib")
    # 시작 시점에 이미 쓰던 스왑은 이 작업 탓이 아니므로 **증가량**을 본다.
    swap_peak = (max(swap_col) - swap_col[0]) if swap_col else None
    psi_full = _peak(rows, "psi_mem_full10", max)
    psi_some = _peak(rows, "psi_mem_some10", max)
    iops_max = _peak(rows, "disk_read_iops", max)
    majf = _peak(rows, "majfault_ps", max)

    ctr = {}
    for r in rows:
        if r.get("containers"):
            try:
                for k, v in json.loads(r["containers"]).items():
                    ctr[k] = max(ctr.get(k, 0), v)
            except ValueError:
                pass

    checks = []

    def chk(name, value, ok, rule):
        checks.append({"name": name, "value": value, "pass": ok, "rule": rule})

    if min_avail is not None:
        chk("MemAvailable 최저(MiB)", round(min_avail), min_avail >= a.min_avail_mib, f">= {a.min_avail_mib}")
    if swap_peak is not None:
        chk("스왑 증가량 최대(MiB)", round(swap_peak), swap_peak <= a.max_swap_mib, f"<= {a.max_swap_mib}")
    if psi_full is not None:
        chk("PSI memory full avg10 최대(%)", psi_full, psi_full <= a.max_psi_full, f"<= {a.max_psi_full}")
        chk("PSI memory some avg10 최대(%)", psi_some, psi_some <= a.max_psi_some, f"<= {a.max_psi_some}")
    else:
        chk("PSI(/proc/pressure)", "없음", False, "커널이 PSI 를 안 줌 — 스래싱을 직접 못 본다(판정 불가)")
    if iops_max is not None:
        cap = a.iops_cap * a.max_iops_frac
        chk("디스크 읽기 IOPS 최대", round(iops_max), iops_max <= cap, f"<= {cap:.0f} (cap {a.iops_cap} x {a.max_iops_frac})")
    if gpu_peak is not None and gpu_total:
        frac = gpu_peak / gpu_total
        chk("VRAM 최대(MiB)", round(gpu_peak), frac <= a.max_vram_frac,
            f"<= {a.max_vram_frac * 100:.0f}% of {gpu_total:.0f}")
    else:
        chk("VRAM", "없음", False, "nvidia-smi 를 못 읽음(판정 불가)")

    phases = []
    if marks:
        bounds = [(marks[i][0], marks[i + 1][0] if i + 1 < len(marks) else float("inf"), marks[i][1])
                  for i in range(len(marks))]
        for lo, hi, label in bounds:
            seg = [r for r in rows if lo <= float(r["t"]) < hi]
            if not seg:
                continue
            phases.append({
                "phase": label, "samples": len(seg),
                "min_avail_mib": _peak(seg, "mem_avail_mib", min),
                "max_psi_full": _peak(seg, "psi_mem_full10", max),
                "max_iops": _peak(seg, "disk_read_iops", max),
                "max_vram_mib": _peak(seg, "gpu_used_mib", max)})

    failed = [c for c in checks if not c["pass"]]
    return {
        "instance_type": meta.get("instance_type", "unknown"), "label": meta.get("label", ""),
        "samples": len(rows), "duration_s": float(rows[-1]["t"]) if rows else 0,
        "mem_total_mib": total, "mem_used_peak_mib": None if total is None or min_avail is None else round(total - min_avail),
        "majfault_ps_max": majf, "containers_peak_mib": dict(sorted(ctr.items(), key=lambda kv: -kv[1])),
        "checks": checks, "phases": phases, "failed": len(failed),
        "verdict": ("기준 통과(초안 기준·이 측정 한 번에 한함)" if not failed else f"기준 미달 {len(failed)}건"),
    }


def render_md(s):
    L = [f"# 호스트 자원 측정 — {s['instance_type']} {s['label']}".rstrip(), "",
         f"- 샘플 {s['samples']}개 / {s['duration_s']:.0f}초, 전체 RAM {s['mem_total_mib']} MiB, "
         f"RAM 사용 피크(전체−MemAvailable 최저) {s['mem_used_peak_mib']} MiB",
         f"- 판정 기준은 **초안**이다(보정 전). 통과해도 이 측정 동안 실행해 본 작업에 한한 결과이며 "
         f"인스턴스 타입을 확정하는 근거가 아니다.", "",
         "| 항목 | 값 | 기준 | 결과 |", "|---|---|---|---|"]
    for c in s["checks"]:
        L.append(f"| {c['name']} | {c['value']} | {c['rule']} | {'PASS' if c['pass'] else '**FAIL**'} |")
    L += ["", f"**{s['verdict']}**", ""]
    if s["containers_peak_mib"]:
        L += ["## 컨테이너별 메모리 피크(docker stats, MiB)", "", "| 컨테이너 | MiB |", "|---|---|"]
        L += [f"| {k} | {v:.0f} |" for k, v in s["containers_peak_mib"].items()] + [""]
    if s["phases"]:
        L += ["## 구간별 최악값", "", "| 구간 | MemAvailable 최저 | PSI full 최대 | IOPS 최대 | VRAM 최대 |", "|---|---|---|---|---|"]
        for p in s["phases"]:
            L.append(f"| {p['phase']} | {p['min_avail_mib']} | {p['max_psi_full']} | {p['max_iops']} | {p['max_vram_mib']} |")
        L.append("")
    return "\n".join(L)


EXIT_PASS, EXIT_FAIL, EXIT_NO_DATA = 0, 3, 4


def cmd_summarize(a):
    path = os.path.join(a.out, "samples.csv")
    rows = list(csv.DictReader(open(path))) if os.path.exists(path) else []
    if not rows:
        print("MEASURE_RESULT=error  (샘플이 없다 — 측정기가 돌지 않았거나 시작하자마자 죽었다)")
        return EXIT_NO_DATA
    marks = []
    mp = os.path.join(a.out, "marks.txt")
    if os.path.exists(mp):
        for line in open(mp):
            t, _, label = line.rstrip("\n").partition("\t")
            marks.append((float(t), label))
    meta = json.load(open(os.path.join(a.out, "meta.json"))) if os.path.exists(os.path.join(a.out, "meta.json")) else {}
    s = summarize_rows(rows, marks, a, meta)
    md = render_md(s)
    open(os.path.join(a.out, "report.md"), "w", encoding="utf-8").write(md)
    json.dump(s, open(os.path.join(a.out, "report.json"), "w"), ensure_ascii=False, indent=1)
    print(md)
    if len(rows) < a.min_samples:
        print(f"MEASURE_RESULT=error  (샘플 {len(rows)}개 < 최소 {a.min_samples}개 — 구간이 너무 짧아 판정할 수 없다)")
        return EXIT_NO_DATA
    if s["failed"]:
        print(f"MEASURE_RESULT=fail  (기준 미달 {s['failed']}건)")
        return EXIT_FAIL
    print("MEASURE_RESULT=pass  (초안 기준·이 측정 한 번에 한함)")
    return EXIT_PASS


def add_thresholds(p):
    p.add_argument("--min-avail-mib", type=float, default=1500)
    p.add_argument("--max-swap-mib", type=float, default=100)
    p.add_argument("--max-psi-full", type=float, default=1.0)
    p.add_argument("--max-psi-some", type=float, default=10.0)
    p.add_argument("--iops-cap", type=float, default=3000, help="gp3 기본 IOPS")
    p.add_argument("--max-iops-frac", type=float, default=0.8)
    p.add_argument("--max-vram-frac", type=float, default=0.90)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample"); s.add_argument("--out", required=True)
    s.add_argument("--interval", type=float, default=2.0)
    s.add_argument("--docker-every", type=int, default=5, help="N 샘플마다 docker stats (0=안 함)")
    s.add_argument("--label", default="")
    m = sub.add_parser("mark"); m.add_argument("--out", required=True); m.add_argument("label")
    z = sub.add_parser("summarize"); z.add_argument("--out", required=True); add_thresholds(z)
    z.add_argument("--min-samples", type=int, default=1, help="이보다 적으면 측정 부족(종료 코드 4)")
    a = ap.parse_args(argv)
    rc = {"sample": cmd_sample, "mark": cmd_mark, "summarize": cmd_summarize}[a.cmd](a)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
