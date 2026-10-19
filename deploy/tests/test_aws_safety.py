"""
AWS 안전장치(35/20 스크립트의 LT·AMI 검증, FalReady 판정, smoke-test 측정 강제)의 성공·실패 경로 검증.
**AWS 는 호출하지 않는다** — 가짜 `aws`/`docker`/`nvidia-smi` 를 PATH 맨 앞에 둔다. fal 호출도 없다.

실행:  python3 deploy/tests/test_aws_safety.py
       ~/miniconda3/envs/sam3d/bin/python deploy/tests/test_aws_safety.py
"""
import base64
import csv
import json
import os
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
sys.path.insert(0, HERE)
sys.path.insert(0, AWS)

from test_aws_fal_deploy import render_userdata  # noqa: E402  (실제 렌더 결과를 쓴다)

SYS_PY3 = shutil.which("python3") or sys.executable


def sh(cmd, env=None, cwd=AWS, inp=None):
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, cwd=cwd,
                          env=env if env is not None else os.environ.copy(), input=inp)


def fal_hash():
    r = subprocess.run(["bash", os.path.join(AWS, "fal-ready.sh"), "hash"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


# ── 가짜 aws ────────────────────────────────────────────────────────────

FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys
D = os.environ["FAKE_DIR"]
st = json.load(open(os.path.join(D, "state.json")))
args = sys.argv[1:]
open(os.path.join(D, "calls.log"), "a").write("aws " + " ".join(args) + "\n")
q = args[args.index("--query") + 1] if "--query" in args else ""
cmd = " ".join(args[:2])
def out(x): print(x); sys.exit(0)
def fail(m="error"): sys.stderr.write(m + "\n"); sys.exit(254)

if cmd == "sts get-caller-identity": out("000000000000")
if cmd == "autoscaling describe-auto-scaling-groups":
    if "DesiredCapacity" in q: out(st["desired"])
    if "length(Instances)" in q: out(st["instances"])
    if "LaunchTemplateSpecification" in q:
        if st.get("spec_fail"): fail("spec fail")
        out(st.get("spec", "emr-gpu-worker\t$Default"))
    if "Overrides" in q: out("g6.2xlarge\tg5.2xlarge")
    if "VPCZoneIdentifier" in q: out("subnet-a,subnet-b")
    out("")
if cmd == "autoscaling update-auto-scaling-group": out("UPDATED")
if cmd == "ec2 describe-subnets": out("ap-northeast-2a")
if cmd == "ec2 describe-instance-type-offerings": out("ap-northeast-2a")
if cmd == "ec2 describe-launch-template-versions":
    if st.get("lt_fail"): fail("lt fail")
    lt = st["lt"]
    out("\t".join(str(lt.get(k, "None")) for k in ("ver", "ami", "ud")))
if cmd == "ec2 describe-images":
    if st.get("images_fail"): fail("images fail")
    im = st["image"]
    if q.startswith("Images[0].[State"): out("\t".join(str(im.get(k, "None")) for k in ("state", "gitsha", "fal")))
    if "FalReady" in q: out(im.get("fal", "None"))
    if "GitSha" in q: out(im.get("gitsha", "None"))
    if "RootDeviceName" in q: out("/dev/sda1")
    if "BlockDeviceMappings" in q: out("150")
    out("")
if cmd == "ec2 describe-launch-templates": out("x")
if cmd == "ec2 create-launch-template-version": out("20")
if cmd == "ec2 create-launch-template": out("lt-1")
out("")
'''


class FakeAws:
    def __init__(self, **state):
        self.dir = tempfile.mkdtemp(prefix="emr-fakeaws-")
        os.makedirs(os.path.join(self.dir, "bin"))
        p = os.path.join(self.dir, "bin", "aws")
        open(p, "w").write(FAKE_AWS)
        os.chmod(p, 0o755)
        base = {"desired": 0, "instances": 0, "lt": {}, "image": {}}
        base.update(state)
        self.set(base)

    def set(self, st):
        json.dump(st, open(os.path.join(self.dir, "state.json"), "w"))

    @property
    def calls(self):
        p = os.path.join(self.dir, "calls.log")
        return open(p).read().splitlines() if os.path.exists(p) else []

    def called(self, needle):
        return [c for c in self.calls if needle in c]

    def env(self, **extra):
        e = {k: v for k, v in os.environ.items()
             if not k.startswith("EMR_") and k not in ("SAM3D_BACKEND", "FAL_KEY", "INSTANCE_TYPES")}
        e["PATH"] = f"{self.dir}/bin:{e['PATH']}"
        e["FAKE_DIR"] = self.dir
        e.update(extra)
        return e

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def b64(text):
    return base64.b64encode(text.encode()).decode()


GOOD_UD = None


def good_userdata():
    global GOOD_UD
    if GOOD_UD is None:
        GOOD_UD = render_userdata("fal")
    return GOOD_UD


def ready_state(**over):
    """검증을 모두 통과하는 LT/AMI 상태. 시험마다 한 군데씩 망가뜨린다."""
    st = {"desired": 0, "instances": 0,
          "lt": {"ver": 21, "ami": "ami-new", "ud": b64(good_userdata())},
          "image": {"state": "available", "gitsha": "abc1234", "fal": fal_hash()}}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(st.get(k), dict):
            st[k].update(v)
        else:
            st[k] = v
    return st


# ── FalReady 공용 함수 ─────────────────────────────────────────────────

def make_repo_copy(modify=None):
    """fal-ready 대상 파일만 담은 임시 git 저장소(커밋됨)."""
    top = tempfile.mkdtemp(prefix="emr-repocopy-")
    files = subprocess.run(["bash", os.path.join(AWS, "fal-ready.sh"), "files"], capture_output=True, text=True,
                           check=True).stdout.split()
    for f in files:
        dst = os.path.join(top, f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(os.path.join(REPO, f), dst)
    if modify:
        modify(top)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    for c in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", top] + c, check=True, capture_output=True, env=env)
    return top


class FalReadyLib(unittest.TestCase):
    def hash_in(self, top):
        r = sh("bash fal-ready.sh hash", env={**os.environ, "EMR_REPO_TOP": top})
        return r.returncode, r.stdout.strip()

    def test_hash_is_deterministic_and_tracks_covered_files(self):
        a = make_repo_copy()
        b = make_repo_copy()
        c = make_repo_copy(lambda t: open(os.path.join(t, "deploy/worker/worker.py"), "a").write("\n# changed\n"))
        try:
            ra, rb, rc_ = self.hash_in(a), self.hash_in(b), self.hash_in(c)
            self.assertEqual(ra[0], 0)
            self.assertEqual(ra, rb)                       # 같은 내용이면 같은 해시
            self.assertNotEqual(ra[1], rc_[1])             # 이미지에 구워지는 worker.py 가 다르면 다른 해시
            self.assertEqual(len(ra[1]), 16)
        finally:
            for t in (a, b, c):
                shutil.rmtree(t, ignore_errors=True)

    def test_hash_fails_when_a_covered_file_is_not_committed(self):
        top = make_repo_copy()
        try:
            subprocess.run(["git", "-C", top, "rm", "-q", "--cached", "sam3d/backend/services/sam3d_fal.py"], check=True)
            subprocess.run(["git", "-C", top, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "rm"],
                           check=True, capture_output=True)
            rc, out = self.hash_in(top)
            self.assertNotEqual(rc, 0)
            self.assertEqual(out, "")
        finally:
            shutil.rmtree(top, ignore_errors=True)

    def test_dirty_tree_detected(self):
        top = make_repo_copy()
        try:
            open(os.path.join(top, "deploy/gpu/Dockerfile"), "a").write("# dirty\n")
            r = sh("source ./fal-ready.sh; fal_ready_clean", env={**os.environ, "EMR_REPO_TOP": top})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("Dockerfile", r.stdout)
        finally:
            shutil.rmtree(top, ignore_errors=True)

    def userdata_check(self, ud):
        return sh("source ./fal-ready.sh; fal_userdata_check", inp=ud)

    def test_real_fal_render_passes_local_render_fails(self):
        self.assertEqual(self.userdata_check(good_userdata()).returncode, 0)
        r = self.userdata_check(render_userdata("local"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("fal 모드 렌더", r.stdout)

    def test_each_missing_safeguard_fails_separately(self):
        ud = good_userdata()
        cases = {
            "기능 표식": ud.replace("emr-userdata-features: fal-ssm gate-off ami-guard v1", "x"),
            "게이트": ud.replace("WARM_GATE_FILE=", "WARM_GATE_X="),
            "가드": ud.replace('[ "fal" != fal ] ||', '[ "fal" = fal ] ||'),
            "SSM 복호화": ud.replace("--with-decryption", ""),
            ".env 백엔드": ud.replace("SAM3D_BACKEND=fal", "SAM3D_BACKEND=local"),
        }
        for name, bad in cases.items():
            self.assertNotEqual(bad, ud, f"{name}: 치환이 안 먹었다(테스트 오류)")
            r = self.userdata_check(bad)
            self.assertNotEqual(r.returncode, 0, f"{name} 가 빠졌는데 통과했다")
            self.assertIn("누락", r.stdout, name)

    def test_marker_alone_is_not_enough(self):
        # 표식만 남기고 코드 조각을 전부 지운 userdata
        r = self.userdata_check("# emr-userdata-features: fal-ssm gate-off ami-guard v1\necho hi\n")
        self.assertNotEqual(r.returncode, 0)

    def test_empty_userdata_fails(self):
        self.assertNotEqual(self.userdata_check("").returncode, 0)


class CheckAmiLocal(unittest.TestCase):
    """빌더 안에서 이미지·체크아웃을 검사하는 부분(가짜 docker)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "bin"))
        self.top = make_repo_copy()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.top, ignore_errors=True)

    def run_check(self, image_ok=True, run_ok=True, top=None):
        d = os.path.join(self.tmp, "bin", "docker")
        open(d, "w").write(textwrap.dedent(f'''\
            #!/bin/bash
            case "$*" in
              *"image inspect"*) exit {0 if image_ok else 1} ;;
              "run "*) exit {0 if run_ok else 1} ;;
            esac
            exit 0
        '''))
        os.chmod(d, 0o755)
        env = {**os.environ, "PATH": f"{self.tmp}/bin:{os.environ['PATH']}",
               "EMR_IMAGE_TAG": "abc1234", "EMR_REPO_DIR": top or self.top}
        return sh("bash fal-ready.sh check-ami-local", env=env)

    def test_all_good(self):
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("✗", r.stdout)

    def test_old_image_without_fal_deps_fails(self):
        r = self.run_check(run_ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("옛 것", r.stdout)

    def test_missing_image_fails(self):
        self.assertNotEqual(self.run_check(image_ok=False).returncode, 0)

    def test_old_checkout_compose_fails(self):
        top = make_repo_copy(lambda t: open(os.path.join(t, "deploy/docker-compose.gpu.yml"), "w").write("services: {}\n"))
        try:
            r = self.run_check(top=top)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("SAM3D_BACKEND", r.stdout)
        finally:
            shutil.rmtree(top, ignore_errors=True)

    def test_old_runner_fails(self):
        top = make_repo_copy(lambda t: open(os.path.join(t, "sam3d/backend/services/sam3d_runner.py"), "w").write("x = 1\n"))
        try:
            self.assertNotEqual(self.run_check(top=top).returncode, 0)
        finally:
            shutil.rmtree(top, ignore_errors=True)

    def test_dirty_checkout_fails(self):
        open(os.path.join(self.top, "deploy/worker/worker.py"), "a").write("# local edit\n")
        r = self.run_check()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("커밋 안 된 수정", r.stdout)

    def test_verify_ami_only_tags_after_check(self):
        txt = open(os.path.join(AWS, "verify-ami.sh"), encoding="utf-8").read()
        self.assertIn("fal_check_ami_local", txt)
        self.assertLess(txt.index("fal_check_ami_local"), txt.index("aws ec2 create-image --region"))
        self.assertIn('FAL_TAG=",{Key=FalReady,Value=$FALH}"', txt)
        # 검사 실패 경로에서는 FAL_TAG 가 빈 채로 남는다
        self.assertIn('FAL_TAG=""', txt)


# ── 35-set-instance-types.sh ───────────────────────────────────────────

class SetInstanceTypes(unittest.TestCase):
    def setUp(self):
        self.fa = FakeAws(**ready_state())

    def tearDown(self):
        self.fa.close()

    def run35(self, **env):
        e = self.fa.env(**env)
        return subprocess.run(["bash", "35-set-instance-types.sh"], capture_output=True, text=True, cwd=AWS, env=e)

    def updates(self):
        return self.fa.called("update-auto-scaling-group")

    TRIAL = {"EMR_GPU_PROFILE": "trial-g6xl", "EMR_TRIAL_ACK": "1"}

    def set_state(self, **over):
        self.fa.set(ready_state(**over))

    # 정상
    def test_new_fal_ami_passes_dry_run_and_apply(self):
        r = self.run35(**self.TRIAL)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("계획만 출력", r.stdout)
        self.assertIn("userdata 안전장치 전부 확인", r.stdout)
        self.assertEqual(self.updates(), [])                 # 기본은 계획만
        r = self.run35(EMR_APPLY="1", **self.TRIAL)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(len(self.updates()), 2)

    # 거부 경로 — 전부 ASG 변경 전에 멈춘다
    def assert_refused(self, why, **over):
        self.set_state(**over)
        r = self.run35(EMR_APPLY="1", **self.TRIAL)
        self.assertNotEqual(r.returncode, 0, f"{why}: 통과해버렸다\n{r.stdout}")
        self.assertEqual(self.updates(), [], f"{why}: ASG 가 변경됐다")
        return r

    def test_old_ami_without_falready_tag_refused(self):
        r = self.assert_refused("옛 AMI", image={"fal": "None"})
        self.assertIn("FalReady", r.stderr)

    def test_ami_with_other_hash_refused_even_if_id_is_new(self):
        r = self.assert_refused("해시 불일치", image={"fal": "0123456789abcdef"})
        self.assertIn("HEAD", r.stderr)

    def test_ami_not_available_refused(self):
        self.assert_refused("pending AMI", image={"state": "pending"})

    def test_userdata_without_guard_refused(self):
        bad = good_userdata().replace('[ "fal" != fal ] ||', '[ "fal" = fal ] ||')
        r = self.assert_refused("가드 없음", lt={"ud": b64(bad)})
        self.assertIn("옛 AMI 차단 가드", r.stderr)

    def test_userdata_without_gate_off_refused(self):
        self.assert_refused("게이트 끔 없음", lt={"ud": b64(good_userdata().replace("WARM_GATE_FILE=", "X="))})

    def test_userdata_fal_without_marker_refused(self):
        # v19 처럼 fal 블록은 있으나 이번 안전장치가 없는 userdata
        bad = good_userdata().replace("emr-userdata-features: fal-ssm gate-off ami-guard v1", "old")
        self.assert_refused("표식 없음", lt={"ud": b64(bad)})

    def test_local_mode_userdata_refused(self):
        self.assert_refused("local userdata", lt={"ud": b64(render_userdata("local"))})

    def test_missing_userdata_refused(self):
        self.assert_refused("userdata 누락", lt={"ud": "None"})

    def test_missing_ami_id_refused(self):
        self.assert_refused("AMI 누락", lt={"ami": "None"})

    def test_undecodable_userdata_refused(self):
        self.assert_refused("깨진 base64", lt={"ud": "###not-base64###"})

    def test_lookup_failures_refused(self):
        for key in ("lt_fail", "images_fail", "spec_fail"):
            self.fa.set(ready_state(**{key: True}))
            r = self.run35(EMR_APPLY="1", **self.TRIAL)
            self.assertNotEqual(r.returncode, 0, key)
            self.assertEqual(self.updates(), [], key)

    def test_trial_requires_ack_and_idle_groups(self):
        self.assertNotEqual(self.run35(EMR_GPU_PROFILE="trial-g6xl", EMR_APPLY="1").returncode, 0)
        self.set_state(desired=1, instances=1)
        r = self.run35(EMR_APPLY="1", **self.TRIAL)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.updates(), [])

    def test_explicit_lt_version_is_resolved_not_default(self):
        self.fa.set(ready_state(spec="emr-gpu-worker\t7"))
        self.run35(**self.TRIAL)
        q = self.fa.called("describe-launch-template-versions")
        self.assertTrue(q and all("--versions 7" in c for c in q), q)

    # stable(롤백)
    def test_stable_rollback_never_blocked_but_reports_lt_and_ami(self):
        # 옛 AMI + 안전장치 없는 userdata(= 현재 v19 와 같은 상태)여도 롤백은 막지 않는다
        old = good_userdata().replace("emr-userdata-features: fal-ssm gate-off ami-guard v1", "old").replace(
            '[ "fal" != fal ] ||', '[ "fal" = fal ] ||')
        self.fa.set(ready_state(lt={"ver": 19, "ami": "ami-old", "ud": b64(old)}, image={"fal": "None", "gitsha": "def0123"}))
        r = self.run35(EMR_APPLY="1")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(len(self.updates()), 2)
        self.assertIn("v19", r.stdout)
        self.assertIn("ami-old", r.stdout)
        self.assertIn("def0123", r.stdout)
        self.assertIn("경고", r.stdout)                       # fal userdata + 미검증 AMI 경고
        self.assertIn("그대로 적용된다", r.stdout)

    def test_stable_rollback_proceeds_even_if_lt_or_ami_lookup_fails(self):
        # 탈출구여야 한다: 정보 조회 실패가 롤백을 막으면 안 된다(대신 확인 못 했다고 알린다)
        for key in ("lt_fail", "images_fail", "spec_fail"):
            self.fa.set(ready_state(**{key: True}))
            r = self.run35(EMR_APPLY="1")
            self.assertEqual(r.returncode, 0, f"{key}: {r.stdout}{r.stderr}")
            self.assertIn("조회하지 못했다", r.stdout)
            self.assertIn("롤백은 계속", r.stdout)
            self.assertEqual(len(self.updates()), 2, key)
            os.remove(os.path.join(self.fa.dir, "calls.log"))

    def test_trial_never_proceeds_on_the_same_lookup_failures(self):
        for key in ("lt_fail", "images_fail", "spec_fail"):
            self.fa.set(ready_state(**{key: True}))
            r = self.run35(EMR_APPLY="1", **self.TRIAL)
            self.assertNotEqual(r.returncode, 0, key)
            self.assertEqual(self.updates(), [], key)
            if os.path.exists(os.path.join(self.fa.dir, "calls.log")):
                os.remove(os.path.join(self.fa.dir, "calls.log"))

    def test_default_is_plan_only(self):
        r = self.run35()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.updates(), [])
        self.assertIn("계획만 출력", r.stdout)


# ── 20-launch-template.sh ──────────────────────────────────────────────

class LaunchTemplateGuard(unittest.TestCase):
    def setUp(self):
        self.fa = FakeAws(image={"state": "available", "gitsha": "abc1234", "fal": "None"})

    def tearDown(self):
        self.fa.close()

    def run20(self, backend="fal", **extra):
        e = self.fa.env(EMR_AMI_ID="ami-x", EMR_SG_ID="sg-x", EMR_SAM3D_BACKEND=backend, **extra)
        return subprocess.run(["bash", "20-launch-template.sh"], capture_output=True, text=True, cwd=AWS, env=e)

    def created(self):
        return self.fa.called("create-launch-template")

    def test_fal_without_falready_tag_aborts_before_creating_anything(self):
        r = self.run20("fal")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FalReady", r.stdout)
        self.assertEqual(self.created(), [])

    def test_fal_with_wrong_hash_aborts(self):
        self.fa.set({"image": {"state": "available", "gitsha": "abc1234", "fal": "ffffffffffffffff"}})
        r = self.run20("fal")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("다릅니다", r.stdout)
        self.assertEqual(self.created(), [])

    def test_fal_with_matching_tag_creates_version(self):
        self.fa.set({"image": {"state": "available", "gitsha": "abc1234", "fal": fal_hash()}})
        r = self.run20("fal")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.fa.called("create-launch-template-version")), 1)
        self.assertIn("fal-ready 확인", r.stdout)

    def test_fal_tag_lookup_failure_aborts(self):
        # GitSha 조회는 `|| echo ""` 로 흐르지만 FalReady 조회 실패는 중단이어야 한다
        self.fa.set({"images_fail": True})
        r = self.run20("fal")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.created(), [])

    def test_local_mode_unchanged_old_ami_ok(self):
        r = self.run20("local")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.fa.called("create-launch-template-version")), 1)
        self.assertNotIn("FalReady", r.stdout)

    def test_rendered_userdata_carries_markers_in_fal(self):
        self.fa.set({"image": {"state": "available", "gitsha": "abc1234", "fal": fal_hash()}})
        self.run20("fal")
        ud = open("/tmp/emr-userdata.rendered.sh", encoding="utf-8").read()
        r = sh("source ./fal-ready.sh; fal_userdata_check", inp=ud)
        self.assertEqual(r.returncode, 0, r.stdout)


# ── measure-resources.sh / measure_resources.py 종료 코드 ───────────────

class MeasureExitCodes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.latest = os.path.join(self.tmp, "latest")
        self.mr = os.path.join(AWS, "measure_resources.py")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def csv_dir(self, n, avail=4000, psi_full=0.0):
        d = os.path.join(self.tmp, f"d{n}-{avail}-{psi_full}")
        os.makedirs(d, exist_ok=True)
        fields = ["t", "mem_total_mib", "mem_avail_mib", "cached_mib", "swap_used_mib", "psi_mem_some10", "psi_mem_full10",
                  "psi_io_some10", "psi_io_full10", "majfault_ps", "disk_read_iops", "gpu_used_mib", "gpu_total_mib",
                  "gpu_util", "load1", "containers"]
        with open(os.path.join(d, "samples.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(fields)
            for i in range(n):
                w.writerow([i * 2, 15368, avail, 0, 0, 0, psi_full, 0, 0, 0, 10, 9000, 23034, 5, 0.1, ""])
        json.dump({"instance_type": "test", "started": 0}, open(os.path.join(d, "meta.json"), "w"))
        return d

    def summarize(self, d, *extra):
        return subprocess.run([sys.executable, self.mr, "summarize", "--out", d, *extra], capture_output=True, text=True)

    def test_exit_codes(self):
        self.assertEqual(self.summarize(self.csv_dir(12)).returncode, 0)                       # 통과
        r = self.summarize(self.csv_dir(12, avail=500))
        self.assertEqual(r.returncode, 3)                                                       # 기준 미달
        self.assertIn("MEASURE_RESULT=fail", r.stdout)
        r = self.summarize(self.csv_dir(12, psi_full=40.0))
        self.assertEqual(r.returncode, 3)

    def test_no_samples_and_too_short_are_errors_not_passes(self):
        empty = os.path.join(self.tmp, "empty"); os.makedirs(empty)
        r = self.summarize(empty)
        self.assertEqual(r.returncode, 4)
        self.assertIn("MEASURE_RESULT=error", r.stdout)
        open(os.path.join(empty, "samples.csv"), "w").write("t,mem_total_mib\n")     # 헤더만
        self.assertEqual(self.summarize(empty).returncode, 4)
        r = self.summarize(self.csv_dir(3), "--min-samples", "10")
        self.assertEqual(r.returncode, 4)
        self.assertIn("너무 짧아", r.stdout)

    def shell(self, *args, **env):
        e = {**os.environ, "EMR_MEASURE_LATEST": self.latest, "EMR_MEASURE_INTERVAL": "0.3", **env}
        return subprocess.run(["bash", os.path.join(AWS, "measure-resources.sh"), *args], capture_output=True, text=True,
                              env=e, timeout=60)

    def test_stop_without_start_is_error(self):
        r = self.shell("stop")
        self.assertEqual(r.returncode, 4)
        self.assertIn("MEASURE_RESULT=error", r.stdout)

    def test_stale_pointer_is_not_read_when_start_fails(self):
        old = self.csv_dir(12)                                     # 지난 측정(통과 상태)
        open(self.latest, "w").write(old)
        blocker = os.path.join(self.tmp, "file"); open(blocker, "w").write("x")
        self.shell("start", EMR_MEASURE_DIR=os.path.join(blocker, "sub"))   # mkdir 실패
        r = self.shell("stop")
        self.assertEqual(r.returncode, 4, r.stdout)

    def test_sampler_crash_is_error(self):
        stub = os.path.join(self.tmp, "bin"); os.makedirs(stub)
        open(os.path.join(stub, "python3"), "w").write("#!/bin/bash\necho boom >&2\nexit 1\n")
        os.chmod(os.path.join(stub, "python3"), 0o755)
        env = {"PATH": f"{stub}:{os.environ['PATH']}", "EMR_MEASURE_DIR": os.path.join(self.tmp, "m1")}
        self.shell("start", **env)
        import time; time.sleep(0.5)
        r = self.shell("stop", **env)
        self.assertEqual(r.returncode, 4, r.stdout)
        self.assertIn("MEASURE_RESULT=error", r.stdout)

    @unittest.skipUnless(os.path.exists("/proc/pressure/memory"), "PSI 없는 호스트")
    def test_real_sampler_pass_and_forced_fail(self):
        import time
        d = os.path.join(self.tmp, "m2")
        self.shell("start", EMR_MEASURE_DIR=d)
        time.sleep(2.0)
        okr = self.shell("stop", EMR_MEASURE_MIN_SAMPLES="2", EMR_MEASURE_MIN_AVAIL_MIB="1",
                         EMR_MEASURE_MAX_VRAM_FRAC="1.0", EMR_MEASURE_MAX_SWAP_MIB="1000000")
        self.assertIn(okr.returncode, (0, 3), okr.stdout)          # 이 PC 의 상태에 따라 PSI/IOPS 가 걸릴 수 있다
        d2 = os.path.join(self.tmp, "m3")
        self.shell("start", EMR_MEASURE_DIR=d2)
        time.sleep(1.5)
        bad = self.shell("stop", EMR_MEASURE_MIN_SAMPLES="2", EMR_MEASURE_MIN_AVAIL_MIB="999999999")
        self.assertEqual(bad.returncode, 3, bad.stdout)            # 기준을 터무니없이 높이면 반드시 미달


# ── smoke-test.sh 최종 판정 ────────────────────────────────────────────

class SmokeEnforce(unittest.TestCase):
    """가짜 docker/aws/nvidia-smi/conda python 으로 smoke-test.sh 를 끝까지 돌린다(스택·AWS·fal 없음)."""

    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="emr-smoke-")
        t = self.t
        os.makedirs(f"{t}/bin")
        for e in ("sam3d", "omni3d", "uLayout"):
            os.makedirs(f"{t}/root/miniconda3/envs/{e}/bin")
            self._w(f"{t}/root/miniconda3/envs/{e}/bin/python", textwrap.dedent('''\
                #!/bin/bash
                case "$*" in
                  *check_gpu_compat*) [ "$FAKE_COMPAT_FAIL_ENV" = "$(basename "$(dirname "$(dirname "$0")")")" ] && { echo "✗ no kernel image"; exit 1; }; echo ok ;;
                  *) echo "[fake e2e]"; exit ${FAKE_E2E_RC:-0} ;;
                esac
            '''))
        os.symlink(REPO, f"{t}/root/Empty_My_Room")
        self._w(f"{t}/bin/docker", textwrap.dedent('''\
            #!/bin/bash
            echo "docker $*" >> "$FAKE_LOG"
            case "$*" in
              *"image inspect"*) exit 0 ;;
              *" logs "*) echo "[worker] 시작 SAM3D_BACKEND=fal"; [ "$FAKE_NO_DIAG" = 1 ] || echo "[fal-diag] ok" ;;
            esac
            exit 0
        '''))
        self._w(f"{t}/bin/nvidia-smi", '#!/bin/bash\ncase "$*" in *query-gpu*) echo "23.0, 23034, 3";; *) echo ok;; esac\n')
        self._w(f"{t}/bin/aws", '#!/bin/bash\necho "aws $*" >> "$FAKE_LOG"; echo SMOKE-SENTINEL-KEY-0001\n')
        self._w(f"{t}/bin/curl", "#!/bin/bash\nexit 0\n")
        self.log = f"{t}/calls.log"
        open(self.log, "w").close()

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    @staticmethod
    def _w(path, text):
        open(path, "w").write(text)
        os.chmod(path, 0o755)

    def run_smoke(self, **env):
        base = {"HOME": os.environ["HOME"], "PATH": f"{self.t}/bin:/usr/bin:/bin", "EMR_ROOT": f"{self.t}/root",
                "EMR_IMAGE_TAG": "abc1234", "FAKE_LOG": self.log, "EMR_MEASURE_LATEST": f"{self.t}/latest",
                "EMR_MEASURE_DIR": f"{self.t}/m-{len(os.listdir(self.t))}", "EMR_MEASURE_INTERVAL": "0.3",
                "EMR_MEASURE_MIN_SAMPLES": "1", "EMR_MEASURE_MIN_AVAIL_MIB": "1",
                "EMR_MEASURE_MAX_VRAM_FRAC": "1.0", "EMR_MEASURE_MAX_SWAP_MIB": "1000000",
                "EMR_MEASURE_MAX_PSI_FULL": "100", "EMR_MEASURE_MAX_PSI_SOME": "100",
                "EMR_MEASURE_MAX_IOPS_FRAC": "1000"}
        base.update(env)
        return subprocess.run(["bash", os.path.join(AWS, "smoke-test.sh")], capture_output=True, text=True, env=base,
                              cwd=REPO, timeout=180)

    def compose_up_calls(self):
        return [l for l in open(self.log).read().splitlines() if "up -d" in l]

    FAIL_MEASURE = {"EMR_MEASURE_MIN_AVAIL_MIB": "999999999"}

    @unittest.skipUnless(os.path.exists("/proc/pressure/memory"), "PSI 없는 호스트")
    def test_enforce_off_measurement_fail_keeps_exit_zero_but_says_so(self):
        r = self.run_smoke(EMR_MEASURE_ENFORCE="0", **self.FAIL_MEASURE)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("기준 미달", r.stdout)
        self.assertIn("강제 모드가 아니라 종료 코드에 반영되지 않았다", r.stdout)       # 혼동 방지 문구
        self.assertIn("작업 검증(e2e)  : 통과", r.stdout)

    @unittest.skipUnless(os.path.exists("/proc/pressure/memory"), "PSI 없는 호스트")
    def test_enforce_on_measurement_fail_exits_3_even_though_e2e_passed(self):
        r = self.run_smoke(EMR_MEASURE_ENFORCE="1", **self.FAIL_MEASURE)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("작업 검증(e2e)  : 통과", r.stdout)
        self.assertIn("전체            : 실패", r.stdout)

    @unittest.skipUnless(os.path.exists("/proc/pressure/memory"), "PSI 없는 호스트")
    def test_enforce_on_all_pass_exits_zero(self):
        r = self.run_smoke(EMR_MEASURE_ENFORCE="1")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("전체            : 성공", r.stdout)
        self.assertNotIn("강제 모드가 아니라", r.stdout)

    def test_enforce_on_sampler_crash_is_not_success(self):
        stub = f"{self.t}/pybin"; os.makedirs(stub)
        self._w(f"{stub}/python3", "#!/bin/bash\nexit 1\n")
        r = self.run_smoke(EMR_MEASURE_ENFORCE="1", PATH=f"{stub}:{self.t}/bin:/usr/bin:/bin")
        self.assertEqual(r.returncode, 4, r.stdout)
        self.assertIn("측정 결과 없음/오류", r.stdout)

    def test_enforce_off_sampler_crash_exit_zero_but_labeled_error(self):
        stub = f"{self.t}/pybin"; os.makedirs(stub)
        self._w(f"{stub}/python3", "#!/bin/bash\nexit 1\n")
        r = self.run_smoke(EMR_MEASURE_ENFORCE="0", PATH=f"{stub}:{self.t}/bin:/usr/bin:/bin")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("측정 결과 없음/오류", r.stdout)
        self.assertIn("종료 코드에 반영되지 않았다", r.stdout)

    def test_enforce_without_measure_is_rejected_before_anything_runs(self):
        r = self.run_smoke(EMR_MEASURE_ENFORCE="1", EMR_MEASURE="0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("측정을 끄고", r.stdout)
        self.assertEqual(self.compose_up_calls(), [])

    def test_trial_profile_defaults_to_enforce_stable_does_not(self):
        r = self.run_smoke(EMR_GPU_PROFILE="trial-g6xl", **self.FAIL_MEASURE)
        self.assertIn("판정 강제(ENFORCE)=1", r.stdout)
        r2 = self.run_smoke(**self.FAIL_MEASURE)
        self.assertIn("판정 강제(ENFORCE)=0", r2.stdout)

    def test_e2e_failure_is_failure_regardless_of_measurement(self):
        r = self.run_smoke(EMR_MEASURE_ENFORCE="0", FAKE_E2E_RC="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("작업 검증(e2e)  : 실패", r.stdout)

    def test_fal_checks_still_fail_the_run(self):
        r = self.run_smoke(EMR_MEASURE_ENFORCE="0", FAKE_NO_DIAG="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[fal-diag] 로그가 없다", r.stdout)

    def test_gpu_compat_failure_stops_before_stack_up(self):
        r = self.run_smoke(FAKE_COMPAT_FAIL_ENV="omni3d")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.compose_up_calls(), [])
        self.assertIn("돌지 않는 CUDA 커널", r.stdout)

    def test_key_never_printed(self):
        r = self.run_smoke(EMR_MEASURE_ENFORCE="0")
        self.assertNotIn("SMOKE-SENTINEL-KEY-0001", r.stdout + r.stderr)
        self.assertNotIn("SMOKE-SENTINEL-KEY-0001", open(self.log).read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
