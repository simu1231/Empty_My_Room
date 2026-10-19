"""
AWS fal 전환 + g6.xlarge 시험 준비의 정적/로컬 검증. AWS 는 건드리지 않는다.

지키려는 것
  1. 로컬 PC 는 SAM3D 로컬 추론이 기본이다(SAM3D_BACKEND 미설정 = local).
  2. AWS 스크립트의 기본은 fal 이고 local 로 되돌릴 수 있다.
  3. stable 프로파일(g6.2xlarge g5.2xlarge)이 기본이고, trial-g6xl 은 fal 에서만 허용된다.
  4. 렌더된 유저데이터가 16384 바이트 한도 안(여유 포함)이고, 키 값이 들어 있지 않으며,
     SSM 읽기 구간이 xtrace 를 끈 채로 돈다.
  5. 타입 변경 스크립트는 계획만 출력하고, desired≠0 이면 거부하고, trial 은 ACK 가 있어야 한다.
  6. 자원 측정 요약이 RAM 압박/PSI 부재를 FAIL 로 잡는다.

이 PC 에는 pytest 도 boto3 도 없어서 unittest 만 쓴다(test_jobspec 머리말 참고).
torch 가 필요한 항목은 sam3d conda 환경에서만 돌고 아니면 건너뛴다.

실행:  python3 deploy/tests/test_aws_fal_deploy.py
       ~/miniconda3/envs/sam3d/bin/python deploy/tests/test_aws_fal_deploy.py   (전부)
"""
import csv
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DEPLOY = os.path.abspath(os.path.join(HERE, ".."))
REPO = os.path.abspath(os.path.join(DEPLOY, ".."))
AWS = os.path.join(DEPLOY, "aws")
sys.path.insert(0, AWS)

USERDATA_LIMIT = 16384
USERDATA_MARGIN = 1500   # 한도에서 이만큼은 비워 둔다(실제 렌더가 로컬 렌더보다 수십 바이트 크다)


def bash(script, env=None, cwd=AWS):
    e = {**os.environ, **(env or {})}
    for k in ("EMR_GPU_PROFILE", "EMR_SAM3D_BACKEND", "INSTANCE_TYPES", "EMR_WARM_SKIP",
              "EMR_WARM_SKIP_BOOT", "SAM3D_BACKEND"):
        if env is None or k not in env:
            e.pop(k, None)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e, cwd=cwd)


def source_config(env=None, expr='echo "$EMR_SAM3D_BACKEND|$EMR_GPU_PROFILE|$INSTANCE_TYPES"'):
    return bash(f'source ./config.sh >/dev/null 2>&1; rc=$?; [ $rc -eq 0 ] && {expr}; exit $rc', env)


class ConfigProfiles(unittest.TestCase):
    def test_defaults_aws_is_fal_and_stable(self):
        r = source_config()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "fal|stable|g6.2xlarge g5.2xlarge")

    def test_aws_can_roll_back_to_local(self):
        r = source_config({"EMR_SAM3D_BACKEND": "local"})
        self.assertEqual(r.stdout.strip(), "local|stable|g6.2xlarge g5.2xlarge")

    def test_trial_profile_uses_g6_xlarge_with_fal(self):
        r = source_config({"EMR_GPU_PROFILE": "trial-g6xl"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "fal|trial-g6xl|g6.xlarge")

    def test_trial_g4dn2xl_profile(self):
        r = source_config({"EMR_GPU_PROFILE": "trial-g4dn2xl"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "fal|trial-g4dn2xl|g4dn.2xlarge")
        self.assertNotEqual(source_config({"EMR_GPU_PROFILE": "trial-g4dn2xl",
                                           "EMR_SAM3D_BACKEND": "local"}).returncode, 0)

    def test_g4dn_xlarge_is_deliberately_not_a_profile(self):
        # RAM 16GiB + VRAM 16GiB 가 둘 다 검증 전이라 후보에 넣지 않았다.
        for name in ("trial-g4dnxl", "trial-g4dn", "g4dn.xlarge"):
            self.assertNotEqual(source_config({"EMR_GPU_PROFILE": name}).returncode, 0, name)

    def test_stable_is_still_default_and_unchanged(self):
        r = source_config({}, 'echo "$EMR_GPU_PROFILE|$INSTANCE_TYPES"')
        self.assertEqual(r.stdout.strip(), "stable|g6.2xlarge g5.2xlarge")

    def test_trial_profile_rejected_with_local_backend(self):
        # local 은 Host RAM 30GB 가 필요해 xlarge(15.4GB)에서 스래싱한다(2026-10-07 실측).
        r = source_config({"EMR_GPU_PROFILE": "trial-g6xl", "EMR_SAM3D_BACKEND": "local"})
        self.assertNotEqual(r.returncode, 0)

    def test_unknown_profile_and_backend_rejected(self):
        self.assertNotEqual(source_config({"EMR_GPU_PROFILE": "g4dn"}).returncode, 0)
        self.assertNotEqual(source_config({"EMR_SAM3D_BACKEND": "bogus"}).returncode, 0)

    def test_warm_skip_boot_drops_sam3d_only_in_fal(self):
        expr = 'echo "BOOT=$EMR_WARM_SKIP_BOOT"; echo "BAKE=$EMR_WARM_SKIP"'
        fal = source_config({"EMR_SAM3D_BACKEND": "fal"}, expr).stdout
        loc = source_config({"EMR_SAM3D_BACKEND": "local"}, expr).stdout
        self.assertIn("sam-3d-objects/checkpoints", fal.split("BAKE=")[0])
        self.assertIn("models--Ruicheng--moge-2-vitl", fal.split("BAKE=")[0])
        self.assertNotIn("sam-3d-objects", loc)
        # bake 쪽(EMR_WARM_SKIP)은 어느 모드에서도 그대로여야 AMI 의 워밍 목록이 완전하다(롤백 대비)
        self.assertNotIn("sam-3d-objects", fal.split("BAKE=")[1])

    def test_warm_skip_regex_filters_baked_list_lines(self):
        """부팅 때 userdata 의 BG_SKIP 정규식이 실제로 SAM3D 줄만 지우는지(구운 목록 형식 그대로)."""
        skip = source_config({"EMR_SAM3D_BACKEND": "fal"}, 'echo "$EMR_WARM_SKIP_BOOT"').stdout.strip()
        pat = "|".join(skip.split())
        lines = [
            "/opt/emr/sam2_repo/checkpoints/sam2.1_hiera_large.pt 0 32",
            "/opt/emr/lama_model/big-lama/models/best.ckpt",
            "/opt/emr/sam-3d-objects/checkpoints/hf/checkpoints/ss_generator.ckpt 0 32",
            "/opt/emr/sam-3d-objects/checkpoints/hf/checkpoints/slat_generator.ckpt 32 32",
            "/opt/emr/.cache/huggingface/hub/models--Ruicheng--moge-2-vitl/blobs/x 0 4",
        ]
        kept = [l for l in lines if not re.search(pat, l)]
        self.assertEqual(kept, lines[:2])


def render_userdata(backend, tag="b768926abcde"):
    """20-launch-template.sh 의 sed 치환을 그대로 뽑아 렌더한다(AWS 호출 없음)."""
    script = f'''
      export EMR_SAM3D_BACKEND={backend}
      source ./config.sh >/dev/null 2>&1 || exit 9
      EMR_IMAGE_TAG={tag}; EXPECT_WORKERS="$Q_SAM3D,$Q_SCENE"; BACKEND_STATE=""
      exprs=$(sed -n '/^sed -e "s|__REGION__/,/userdata.sh > /p' 20-launch-template.sh \\
                | sed 's/^sed //; s/ *userdata.sh > .*//; s/ \\\\$//')
      eval "sed $(echo "$exprs" | tr '\\n' ' ') userdata.sh"
    '''
    r = bash(script)
    assert r.returncode == 0, r.stderr
    return r.stdout


class UserdataRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fal = render_userdata("fal")
        cls.loc = render_userdata("local")

    def test_size_with_margin(self):
        for name, ud in (("fal", self.fal), ("local", self.loc)):
            n = len(ud.encode("utf-8"))
            self.assertLessEqual(n, USERDATA_LIMIT - USERDATA_MARGIN, f"{name} 렌더 {n}바이트")

    def test_no_unresolved_placeholders(self):
        for ud in (self.fal, self.loc):
            self.assertIsNone(re.search(r"__[A-Z0-9_]+__", ud))

    def test_bash_syntax_ok(self):
        for ud in (self.fal, self.loc):
            r = subprocess.run(["bash", "-n"], input=ud, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_ssm_block_present_and_traceoff_before_aws(self):
        self.assertIn('if [ "fal" = fal ]; then', self.fal)
        self.assertIn('if [ "local" = fal ]; then', self.loc)   # local 이면 이 구간은 실행되지 않는다
        i_off = self.fal.index("set +x")
        i_ssm = self.fal.index("aws ssm get-parameter")
        i_on = self.fal.index("set -x", i_ssm)
        self.assertLess(i_off, i_ssm)
        self.assertLess(i_ssm, i_on)
        self.assertIn("--with-decryption", self.fal)
        self.assertIn("/sam3d/FAL_KEY", self.fal)
        self.assertIn("WARM_GATE_FILE=", self.fal)          # fal 이면 게이트를 끈다

    def test_key_never_stored_in_a_variable_or_literal(self):
        for ud in (self.fal, self.loc):
            self.assertIsNone(re.search(r"FAL_KEY=\$\(", ud), "키를 변수에 담으면 xtrace 가 찍는다")
            self.assertIsNone(re.search(r"FAL_KEY=[A-Za-z0-9_-]{8,}", ud), "키 리터럴")

    def test_stale_ami_guard_only_blocks_fal(self):
        self.assertIn('[ "fal" != fal ]', self.fal)
        self.assertIn('[ "local" != fal ]', self.loc)    # local 은 옛 AMI 와도 같이 쓸 수 있어야 한다

    def test_fal_block_failure_aborts(self):
        """aws 가 실패하면 set -e 로 중단되고 xtrace 에 키가 안 찍힌다(가짜 aws 로 실행)."""
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "bin")); os.makedirs(os.path.join(tmp, "deploy"))
            fake = os.path.join(tmp, "bin", "aws")
            open(fake, "w").write('#!/bin/bash\n[ "$FAKE_FAIL" = 1 ] && exit 254\necho "TOPSECRET-VALUE-123"\n')
            os.chmod(fake, 0o755)
            i = self.fal.index("# fal 이면"); j = self.fal.index("\nfi\n", i) + 4
            blk = "set -euxo pipefail\numask 077\necho S3_BUCKET=x > deploy/.env\nSTEP=a\n" + self.fal[i:j] + "echo AFTER\n"
            env = {**os.environ, "PATH": f"{tmp}/bin:{os.environ['PATH']}"}
            ok = subprocess.run(["bash", "-c", blk], capture_output=True, text=True, cwd=tmp, env=env)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertNotIn("TOPSECRET", ok.stdout + ok.stderr)
            envtxt = open(os.path.join(tmp, "deploy/.env")).read()
            self.assertIn("SAM3D_BACKEND=fal", envtxt); self.assertIn("FAL_KEY=TOPSECRET-VALUE-123", envtxt)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(tmp, "deploy/.env")).st_mode), 0o600)
            open(os.path.join(tmp, "deploy/.env"), "w").write("S3_BUCKET=x\n")
            bad = subprocess.run(["bash", "-c", blk], capture_output=True, text=True, cwd=tmp,
                                 env={**env, "FAKE_FAIL": "1"})
            self.assertNotEqual(bad.returncode, 0)
            self.assertNotIn("AFTER", bad.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class MeasureResources(unittest.TestCase):
    def setUp(self):
        import measure_resources as mr
        self.mr = mr
        self.args = type("A", (), dict(min_avail_mib=1500, max_swap_mib=100, max_psi_full=1.0, max_psi_some=10.0,
                                       iops_cap=3000, max_iops_frac=0.8, max_vram_frac=0.9))()

    def rows(self, avail, psi_full=0.0, psi_some=0.0, swap=0, iops=50, vram=9000, total=15368, vram_total=23034):
        out = []
        for i, a in enumerate(avail):
            out.append({"t": str(i * 2), "mem_total_mib": str(total), "mem_avail_mib": str(a), "cached_mib": "0",
                        "swap_used_mib": str(swap if i else 0), "psi_mem_some10": str(psi_some),
                        "psi_mem_full10": str(psi_full), "psi_io_some10": "0", "psi_io_full10": "0",
                        "majfault_ps": "0", "disk_read_iops": str(iops), "gpu_used_mib": str(vram),
                        "gpu_total_mib": str(vram_total), "gpu_util": "10", "load1": "0.1", "containers": ""})
        return out

    def summarize(self, rows, marks=()):
        return self.mr.summarize_rows(rows, list(marks), self.args, {"instance_type": "g6.xlarge"})

    def test_healthy_run_passes_but_is_labeled_provisional(self):
        s = self.summarize(self.rows([4000, 3500, 3800]))
        self.assertEqual(s["failed"], 0)
        self.assertIn("초안", s["verdict"])
        self.assertIn("확정", self.mr.render_md(s))      # 운영 확정 근거가 아니라는 문구

    def test_ram_exhaustion_fails(self):
        s = self.summarize(self.rows([4000, 900, 3000]))
        self.assertTrue(any(c["name"].startswith("MemAvailable") and not c["pass"] for c in s["checks"]))

    def test_thrash_signature_fails(self):
        # g6.xlarge + SAM3D 콜드에서 본 모양: PSI full 상승 + 디스크 읽기가 gp3 상한에 붙음
        s = self.summarize(self.rows([1200, 1100, 1300], psi_full=35.0, psi_some=60.0, iops=3200))
        failed = {c["name"] for c in s["checks"] if not c["pass"]}
        self.assertTrue(any("PSI memory full" in n for n in failed))
        self.assertTrue(any("IOPS" in n for n in failed))

    def test_preexisting_swap_is_not_blamed(self):
        rows = self.rows([4000, 3900])
        for r in rows:
            r["swap_used_mib"] = "2352"          # 시작부터 쓰던 스왑
        s = self.summarize(rows)
        self.assertTrue(next(c for c in s["checks"] if "스왑" in c["name"])["pass"])

    def test_swap_growth_fails(self):
        s = self.summarize(self.rows([4000, 3900], swap=800))
        self.assertFalse(next(c for c in s["checks"] if "스왑" in c["name"])["pass"])

    def test_missing_psi_or_gpu_is_unjudgeable_not_pass(self):
        rows = self.rows([4000, 3900])
        for r in rows:
            r["psi_mem_full10"] = r["psi_mem_some10"] = ""
            r["gpu_used_mib"] = r["gpu_total_mib"] = ""
        s = self.summarize(rows)
        self.assertGreaterEqual(s["failed"], 2)

    def test_vram_limit(self):
        s = self.summarize(self.rows([4000], vram=22000))      # 95% of 23034
        self.assertFalse(next(c for c in s["checks"] if c["name"].startswith("VRAM"))["pass"])

    def test_phases_report_worst_per_phase(self):
        rows = self.rows([4000, 3900, 1800, 1700, 3900, 4000])
        s = self.summarize(rows, marks=[(0.0, "stack-up"), (4.0, "e2e")])
        by = {p["phase"]: p for p in s["phases"]}
        self.assertEqual(by["stack-up"]["min_avail_mib"], 3900)
        self.assertEqual(by["e2e"]["min_avail_mib"], 1700)

    def test_sampler_cli_roundtrip(self):
        out = tempfile.mkdtemp()
        try:
            p = subprocess.run([sys.executable, os.path.join(AWS, "measure_resources.py"), "sample", "--out", out,
                                "--interval", "0.3", "--docker-every", "0"],
                               capture_output=True, text=True, timeout=5) if False else None
            proc = subprocess.Popen([sys.executable, os.path.join(AWS, "measure_resources.py"), "sample", "--out", out,
                                     "--interval", "0.3", "--docker-every", "0"])
            import time; time.sleep(1.5); proc.terminate(); proc.wait(timeout=5)
            subprocess.run([sys.executable, os.path.join(AWS, "measure_resources.py"), "mark", "--out", out, "x"], check=True)
            r = subprocess.run([sys.executable, os.path.join(AWS, "measure_resources.py"), "summarize", "--out", out],
                               capture_output=True, text=True)
            self.assertIn(r.returncode, (0, 3), r.stdout + r.stderr)    # 0 통과 / 3 기준 미달. 4(측정 없음)면 실패
            self.assertTrue(os.path.exists(os.path.join(out, "report.md")))
            self.assertGreaterEqual(len(list(csv.DictReader(open(os.path.join(out, "samples.csv"))))), 2)
        finally:
            shutil.rmtree(out, ignore_errors=True)


def _load_e2e_helpers():
    """test_e2e.py 는 import 하면 실행되므로, 순수 함수 부분만 잘라서 올린다."""
    src = open(os.path.join(DEPLOY, "test_e2e.py"), encoding="utf-8").read()
    i = src.index("def sam3d_mode():")
    j = src.index("def check_sam3d_payload")
    k = src.index("\nimport subprocess", j)
    ns = {"os": os, "sys": sys}
    exec(src[i:k], ns)
    return ns


class E2EHelpers(unittest.TestCase):
    def setUp(self):
        self.ns = _load_e2e_helpers()
        self._env = {k: os.environ.get(k) for k in ("E2E_SAM3D", "SAM3D_BACKEND", "E2E_SAM3D_RUNS")}
        for k in self._env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_default_is_local_like_before(self):
        self.assertEqual(self.ns["sam3d_mode"](), "local")
        self.assertEqual([j[0] for j in self.ns["build_jobs"]("local")],
                         ["sam3d_mesh"] * 3 + ["room_layout", "omni3d"])

    def test_follows_backend_env(self):
        os.environ["SAM3D_BACKEND"] = "fal"
        self.assertEqual(self.ns["sam3d_mode"](), "fal")
        os.environ["E2E_SAM3D"] = "skip"
        self.assertEqual(self.ns["sam3d_mode"](), "skip")

    def test_fal_runs_once_by_default_and_skip_has_none(self):
        self.assertEqual([j[0] for j in self.ns["build_jobs"]("fal")], ["sam3d_mesh", "room_layout", "omni3d"])
        os.environ["E2E_SAM3D_RUNS"] = "2"
        self.assertEqual(sum(1 for j in self.ns["build_jobs"]("fal") if j[0] == "sam3d_mesh"), 2)
        self.assertEqual([j[0] for j in self.ns["build_jobs"]("skip")], ["room_layout", "omni3d"])

    def test_uLayout_and_omni3d_always_present(self):
        for mode in ("local", "fal", "skip"):
            types = [j[0] for j in self.ns["build_jobs"](mode)]
            self.assertIn("room_layout", types); self.assertIn("omni3d", types)

    # ── sam3d 결과 스키마 ──
    @staticmethod
    def textured(**over):
        p = {"success": True, "type": "textured", "backend": "fal", "sam3d_size_m": None,
             "mesh": {"vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "faces": [[0, 1, 2]],
                      "uvs": [[0, 0], [1, 0], [0, 1]], "textureB64": "AAAA"}}
        p.update(over)
        return p

    @staticmethod
    def vertex_color():
        return {"success": True, "type": "vertex_color",
                "mesh": {"vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "faces": [[0, 1, 2]],
                         "colors": [[.5, .5, .5]] * 3}}

    def test_fal_and_local_success_shapes_pass(self):
        chk = self.ns["check_sam3d_payload"]
        self.assertTrue(chk(self.textured(), "fal")[0])
        self.assertTrue(chk(self.vertex_color(), "local")[0])         # backend 필드 없음 = 정상(필수 아님)
        self.assertTrue(chk(self.textured(sam3d_size_m={"width": 1, "height": 2.5, "depth": 3}), "fal")[0])

    def test_null_or_missing_size_is_not_required(self):
        chk = self.ns["check_sam3d_payload"]
        p = self.textured(); del p["sam3d_size_m"]
        self.assertTrue(chk(p, "fal")[0])
        self.assertTrue(chk(self.textured(sam3d_size_m=None), "fal")[0])
        self.assertFalse(chk(self.textured(sam3d_size_m={"width": "x"}), "fal")[0])   # 있다면 모양은 맞아야 한다

    def test_backend_marker_rules(self):
        chk = self.ns["check_sam3d_payload"]
        self.assertFalse(chk(self.textured(backend=None), "fal")[0])           # fal 인데 로컬 경로로 돔
        self.assertFalse(chk(self.textured(), "local")[0])                      # local 인데 fal 결과

    def test_wrong_structures_fail(self):
        chk = self.ns["check_sam3d_payload"]
        bad = {
            "success false": self.textured(success=False),
            "type 이상": self.textured(type="glb"),
            "mesh 없음": self.textured(mesh=None),
            "정점 없음": self.textured(mesh={"vertices": [], "faces": [[0, 1, 2]], "uvs": [], "textureB64": "A"}),
            "면 없음": self.textured(mesh={"vertices": [[0, 0, 0]], "faces": [], "uvs": [[0, 0]], "textureB64": "A"}),
            "정점이 3차원 아님": self.textured(mesh={"vertices": [[0, 0]], "faces": [[0, 0, 0]], "uvs": [[0, 0]], "textureB64": "A"}),
            "uv 개수 불일치": self.textured(mesh={"vertices": [[0, 0, 0]] * 3, "faces": [[0, 1, 2]], "uvs": [[0, 0]], "textureB64": "A"}),
            "텍스처 없음": self.textured(mesh={"vertices": [[0, 0, 0]], "faces": [[0, 0, 0]], "uvs": [[0, 0]], "textureB64": ""}),
        }
        for name, p in bad.items():
            self.assertFalse(chk(p, "fal")[0], name)
        vc = self.vertex_color(); vc["mesh"]["colors"] = [[.5, .5, .5]]
        self.assertFalse(chk(vc, "local")[0])                                    # colors 개수 불일치
        self.assertFalse(chk([], "fal")[0])                                      # 객체 아님

    # ── done 응답 계약(result_url) ──
    class _Resp:
        def __init__(self, status=200, body=None, raw=None):
            self.status_code, self._b, self.content = status, body, (raw if raw is not None else b"{}")

        def json(self):
            if isinstance(self._b, Exception):
                raise self._b
            return self._b

    def test_done_without_result_url_fails(self):
        v = self.ns["validate_done"]
        for res in ({"status": "done"}, {"status": "done", "result_url": ""}, {"status": "done", "result_url": None},
                    {"status": "done", "result_url": "s3://bucket/key"}):
            ok, payload, why = v(res, lambda u: self._Resp(body={"success": True}))
            self.assertFalse(ok, res)
            self.assertIsNone(payload)
            self.assertIn("result_url", why)

    def test_done_download_problems_fail(self):
        v = self.ns["validate_done"]
        res = {"status": "done", "result_url": "http://x/y"}
        self.assertFalse(v(res, lambda u: self._Resp(status=403))[0])                         # 만료/권한
        self.assertFalse(v(res, lambda u: self._Resp(body=ValueError("not json"), raw=b"<xml/>"))[0])
        self.assertFalse(v(res, lambda u: self._Resp(body=[1, 2]))[0])                        # 객체 아님
        def boom(u): raise OSError("net down")
        ok, _, why = v(res, boom)
        self.assertFalse(ok)
        self.assertIn("다운로드 실패", why)

    def test_done_ok_returns_payload(self):
        v = self.ns["validate_done"]
        ok, payload, why = v({"status": "done", "result_url": "https://s3/x?sig=1"}, lambda u: self._Resp(body={"a": 1}))
        self.assertTrue(ok)
        self.assertEqual(payload, {"a": 1})

    def test_skip_mode_has_no_sam3d_jobs_so_no_sam3d_check(self):
        self.assertNotIn("sam3d_mesh", [j[0] for j in self.ns["build_jobs"]("skip")])


class ComposeGate(unittest.TestCase):
    """docker-compose.aws.yml 의 WARM_GATE_FILE: 값 없음=기존 경로, 빈 값=게이트 끔."""

    def render(self, extra_env):
        files = ["docker-compose.yml", "docker-compose.gpu.yml", "docker-compose.aws.yml"]
        cmd = ["docker", "compose"] + sum([["-f", f] for f in files], []) + ["config", "--format", "json"]
        env = {**os.environ, "S3_BUCKET": "b", "DDB_TABLE": "t", "EMR_ROOT": "/opt/emr"}
        for k in ("WARM_GATE_FILE", "SAM3D_BACKEND", "FAL_KEY"):
            env.pop(k, None)
        env.update(extra_env)
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=DEPLOY, env=env)
        if r.returncode != 0:
            raise unittest.SkipTest(f"docker compose config 불가: {r.stderr[:120]}")
        return json.loads(r.stdout)["services"]

    def test_gate_default_and_disabled(self):
        try:
            svc = self.render({})
        except FileNotFoundError:
            self.skipTest("docker 없음")
        self.assertEqual(svc["worker-sam3d"]["environment"]["WARM_GATE_FILE"], "/warm/warm-bg.done")
        off = self.render({"WARM_GATE_FILE": ""})
        self.assertEqual(off["worker-sam3d"]["environment"]["WARM_GATE_FILE"], "")

    def test_backend_defaults_to_local_and_reaches_both_services(self):
        try:
            svc = self.render({})
        except FileNotFoundError:
            self.skipTest("docker 없음")
        for name in ("worker-sam3d", "backend"):
            self.assertEqual(svc[name]["environment"]["SAM3D_BACKEND"], "local", name)
        fal = self.render({"SAM3D_BACKEND": "fal", "FAL_KEY": "k-test"})
        for name in ("worker-sam3d", "backend"):
            self.assertEqual(fal[name]["environment"]["SAM3D_BACKEND"], "fal", name)


try:
    import torch  # noqa: F401
    HAVE_TORCH = True
except Exception:
    HAVE_TORCH = False


@unittest.skipUnless(HAVE_TORCH, "torch 가 있는 sam3d 환경에서만")
class LocalDefaultUnchanged(unittest.TestCase):
    """개발 PC 의 기본 동작: 환경변수가 없으면 로컬 GPU 경로(generate)를 부른다."""

    def setUp(self):
        sys.path.insert(0, os.path.join(REPO, "sam3d", "backend"))
        os.environ.pop("SAM3D_BACKEND", None)
        from services import sam3d_runner
        self.r = sam3d_runner

    def test_default_backend_is_local(self):
        self.assertEqual(self.r.sam3d_backend(), "local")

    def test_local_dispatches_to_generate_and_never_touches_fal(self):
        calls = {}
        sys.modules.pop("services.sam3d_fal", None)   # 다른 테스트가 import 해 뒀을 수 있다
        orig = self.r.generate
        self.r.generate = lambda p, b, c: calls.setdefault("g", (p, b, c)) and {"success": True}
        try:
            out = self.r.run_sam3d(b"img", "의자", pipeline="PIPE")
        finally:
            self.r.generate = orig
        self.assertEqual(calls["g"], ("PIPE", b"img", "의자"))
        self.assertNotIn("services.sam3d_fal", sys.modules)

    def test_local_without_pipeline_is_an_error_not_a_silent_fal(self):
        with self.assertRaises(ValueError):
            self.r.run_sam3d(b"img", "의자")

    def test_fal_mode_does_not_load_local_pipeline_in_worker(self):
        try:
            import orjson  # noqa: F401  (worker 가 결과 직렬화에 쓴다)
        except ImportError:
            self.skipTest("orjson 없음 — sam3d 환경에서만")
        os.environ["SAM3D_BACKEND"] = "fal"
        try:
            sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(DEPLOY, "worker"))
            from test_jobspec import _install_fake_boto3
            _install_fake_boto3()
            import worker
            loaded = []
            worker._get_pipeline = lambda: loaded.append(1) or object()
            from services import sam3d_fal
            orig = sam3d_fal.generate_fal
            sam3d_fal.generate_fal = lambda b, c: {"success": True, "backend": "fal", "mesh": {}}
            tmp = tempfile.NamedTemporaryFile(delete=False); tmp.write(b"x"); tmp.close()
            try:
                body, ctype = worker._run_sam3d_mesh({"params": {"category": "소파"}}, tmp.name, {})
            finally:
                sam3d_fal.generate_fal = orig; os.unlink(tmp.name)
            self.assertEqual(loaded, [], "fal 모드인데 워커가 로컬 파이프라인을 로드했다")
            self.assertEqual(ctype, "application/json")
        finally:
            os.environ.pop("SAM3D_BACKEND", None)


SAM3D_PY = os.path.expanduser("~/miniconda3/envs/sam3d/bin/python")


@unittest.skipUnless(os.path.exists(SAM3D_PY), "sam3d conda 환경 없음")
class GpuCompatCheck(unittest.TestCase):
    """check_gpu_compat.py 자체의 동작. 이 PC 의 GPU(sm_89)에서만 의미가 있다 — T4(sm_75) 결과가 아니다."""

    def run_check(self, env, py=SAM3D_PY):
        e = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        return subprocess.run([py, "-W", "ignore", os.path.join(AWS, "check_gpu_compat.py"), "--env", env],
                              capture_output=True, text=True, env=e, timeout=120)

    def cuda_available(self):
        r = subprocess.run([SAM3D_PY, "-c", "import torch,sys;sys.exit(0 if torch.cuda.is_available() else 3)"],
                           capture_output=True)
        return r.returncode == 0

    def test_passes_on_this_gpu_and_prints_capability(self):
        if not self.cuda_available():
            self.skipTest("이 호스트에 CUDA 없음")
        r = self.run_check("sam3d")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"sm_\d+")
        self.assertIn("전부 통과", r.stdout)

    def test_missing_library_is_reported_as_failure_not_silently_passed(self):
        if not self.cuda_available():
            self.skipTest("이 호스트에 CUDA 없음")
        r = self.run_check("omni3d")           # sam3d 환경에는 detectron2 가 없다
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("detectron2", r.stdout)

    def test_smoke_runs_compat_before_stack_up(self):
        txt = open(os.path.join(AWS, "smoke-test.sh"), encoding="utf-8").read()
        self.assertLess(txt.index("check_gpu_compat.py"), txt.index("docker compose $CF up"))
        for env in ("sam3d", "omni3d", "uLayout"):
            self.assertIn(env, txt[txt.index("GPU 호환 사전 점검"):txt.index("SAM3D 백엔드 ─")])


class SecretHygiene(unittest.TestCase):
    TRACKED_GLOBS = ["deploy", "sam3d/backend", "tools"]

    def test_no_key_like_literals_in_changed_files(self):
        pats = [re.compile(r"AKIA[0-9A-Z]{16}"),
                re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{32}"),
                re.compile(r"FAL_KEY\s*[:=]\s*[\"']?[A-Za-z0-9_-]{20,}")]
        files = ["deploy/aws/userdata.sh", "deploy/aws/config.sh", "deploy/aws/smoke-test.sh",
                 "deploy/aws/measure-resources.sh", "deploy/aws/35-set-instance-types.sh",
                 "deploy/aws/check_gpu_compat.py",
                 "deploy/aws/10-iam.sh", "deploy/docker-compose.gpu.yml", "deploy/docker-compose.aws.yml",
                 "deploy/test_e2e.py", "sam3d/backend/services/sam3d_fal.py"]
        for f in files:
            txt = open(os.path.join(REPO, f), encoding="utf-8").read()
            for p in pats:
                self.assertIsNone(p.search(txt), f"{f}: {p.pattern}")

    def test_dotenv_is_gitignored(self):
        for path in (".env", "deploy/.env", "sam3d/backend/.env"):
            r = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO)
            self.assertEqual(r.returncode, 0, f"{path} 가 .gitignore 에 없다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
