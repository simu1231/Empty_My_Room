#!/bin/bash
# **개발 PC에서** 돈다. AMI 원본 인스턴스가 받아갈 것들을 S3에 올린다.
#
# 왜 "다시 설치"가 아니라 "그대로 옮기기"인가
# ──────────────────────────────────────────────────────────────────────────
# 처음에는 EC2에서 받을 수 있는 건 받게 하려 했다(집 회선으로 51GB를 올리는 것보다
# AWS 안에서 받는 게 빠르고 공짜다). 실제 상태를 들여다보고 포기했다:
#
#   · conda 환경 3개에 재현 명세가 없다. environment.yml도 requirements 고정본도
#     없고, 손으로 패치해 가며 맞춘 결과물이다. `conda create`는 그날 인덱스에 따라
#     다른 버전을 고른다.
#   · 소스 3개에 커밋되지 않은 수정이 있다(sam-3d-objects 7파일 + depth_pro.py 신규,
#     uLayout room_rectify.py, omni3d는 server.py/configs/tools가 전부 untracked).
#     `git clone`은 이걸 하나도 안 가져오는데 clone 자체는 "성공"한다.
#   · HF 캐시가 **새로 받아서는 재현되지 않는 상태**다. sam-3d-objects의 snapshots/
#     디렉터리는 비어 있고, 실제 가중치 12.3GB는 blobs/ 에만 있다. 파이프라인은
#     sam-3d-objects/checkpoints/hf/checkpoints/*.ckpt 를 읽는데 그게 blob 파일명
#     (sha256)으로 걸린 심링크다. 새로 받으면 snapshots/가 제대로 채워지는 대신
#     blob 이름이 달라질 수 있고, 그러면 이 심링크 7개가 끊긴다 — AMI를 구운 뒤
#     첫 추론에서야 안다.
#   · facebook/sam-3d-objects 는 게이트 저장소다. 받으려면 인스턴스에 HF 토큰을
#     올려야 한다. 안 올리는 쪽이 낫다.
#   · runwayml/stable-diffusion-inpainting 은 허브에서 내려갔을 수 있다(4GB).
#
# 그래서 전부 올린다(≈51GB). 한 번 치르는 비용이고, S3 수신은 공짜다. 대신
# 외부 의존이 0이 된다 — 게이트도, 토큰도, 삭제된 저장소도, 해시 의존도 없다.
#
#   올리는 것                     크기      비고
#   ──────────────────────────────────────────────────────────────────────
#   conda 환경 3개 (tar.gz)       ≈17GB     원본 39GB. conda-pack로 경로까지 바꿔 포장
#   HF 캐시 (tar)                 27.8GB    blob/심링크 구조를 그대로 보존
#   torch hub 캐시 (tar)          3.4GB     ZoeDepth/DINOv2/PerspectiveFields/resnet50
#   체크포인트 3개                2.9GB     depth_pro / best_mp3d / cubercnn
#   소스 트리 5개 (tar.gz)        339MB     .git 제외, 작업 트리 그대로
#
# 운 좋은 점 하나 — checkpoints/hf/checkpoints 의 심링크는 `../../../../.cache/...`
# 즉 **상대 경로**다. 루트가 $HOME에서 /opt/emr 로 바뀌어도 같은 상대 위치를
# 가리키므로 그대로 성립한다. 손댈 필요가 없다.
set -euo pipefail
cd "$(dirname "$0")"
. ./config.sh

SRC_ROOT=${EMR_SRC_ROOT:-$HOME}          # 지금 모델이 실제로 있는 곳(개발 PC)
PREFIX=${EMR_AMI_PREFIX:-ami/v1}         # S3 키 접두사
S3=s3://$S3_BUCKET/$PREFIX

ENVS="sam3d uLayout omni3d"
SRC_REPOS="sam-3d-objects uLayout omni3d detectron2 pytorch3d_omni3d_build"
CKPTS="sam-3d-objects/checkpoints/depth_pro/depth_pro.pt
uLayout/ckpt/best_mp3d.pth
omni3d/checkpoints/indoor/cubercnn_DLA34_FPN.pth"

# ── 스테이징 위치 ────────────────────────────────────────────────────────
# WSL2에서 이게 함정이다. `df /` 는 여유 779GB라고 하지만 그 ext4는 C드라이브 위의
# vhdx 파일이고, C드라이브에는 35GB밖에 안 남아 있다. WSL 안에 28GB짜리 tar를
# 만들면 vhdx가 그만큼 커지면서 C를 채우고, 그때부터 CUDA가 엉뚱한 오류를 낸다
# (전에 한 번 당했다 — dmesg의 dxg 줄에 -75가 찍힌다). 기본값은 vhdx 밖인 /mnt/d.
if [ -z "${EMR_STAGE:-}" ]; then
  if [ -d /mnt/d ] && [ "$(stat -f -c %T /mnt/d 2>/dev/null)" != "ext2/ext3" ]; then
    EMR_STAGE=/mnt/d/emr-ami-stage
  else
    EMR_STAGE=/tmp/emr-ami-stage
  fi
fi
STAGE=$EMR_STAGE
mkdir -p "$STAGE"

# 하나씩 만들어 올리고 바로 지우므로 동시에 필요한 공간은 "가장 큰 조각 하나"
# = HF 캐시 tar 28GB다. 넉넉히 35GB를 요구한다.
FREE_GB=$(df -BG --output=avail "$STAGE" | tail -1 | tr -dc 0-9)
echo "▶ 스테이징 $STAGE (여유 ${FREE_GB}GB)"
[ "$FREE_GB" -ge 35 ] || {
  echo "✗ 여유 공간이 ${FREE_GB}GB뿐입니다 — 35GB 이상 필요합니다."
  echo "  EMR_STAGE=/다른/디스크/경로 $0 로 바꿔 지정하세요."; exit 1; }

# ── 사전 점검 ────────────────────────────────────────────────────────────
command -v aws >/dev/null || { echo "✗ aws CLI가 없습니다"; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || {
  echo "✗ AWS 자격증명이 없습니다 — aws configure 를 먼저 하세요"; exit 1; }
CONDA_PACK=$(command -v conda-pack || echo "$SRC_ROOT/miniconda3/bin/conda-pack")
[ -x "$CONDA_PACK" ] || { echo "✗ conda-pack이 없습니다: pip install conda-pack"; exit 1; }

for e in $ENVS; do
  [ -d "$SRC_ROOT/miniconda3/envs/$e" ] || { echo "✗ 환경 $e 가 없습니다"; exit 1; }
done
for r in $SRC_REPOS; do
  [ -d "$SRC_ROOT/$r" ] || { echo "✗ 소스 $r 가 없습니다"; exit 1; }
done
while read -r c; do
  [ -f "$SRC_ROOT/$c" ] || { echo "✗ 체크포인트 $c 가 없습니다"; exit 1; }
done <<< "$CKPTS"
[ -d "$SRC_ROOT/.cache/huggingface" ] || { echo "✗ HF 캐시가 없습니다"; exit 1; }

# 파이프라인이 실제로 읽는 7개 심링크가 멀쩡한지 본다. 하나라도 끊겨 있으면
# 지금 이 PC에서도 추론이 안 되는 상태이고, 그걸 올려봐야 의미가 없다.
HFCK=$SRC_ROOT/sam-3d-objects/checkpoints/hf/checkpoints
[ -f "$HFCK/pipeline.yaml" ] || { echo "✗ $HFCK/pipeline.yaml 이 없습니다"; exit 1; }
for l in "$HFCK"/*.ckpt "$HFCK"/*.pt; do
  [ -e "$l" ] || { echo "✗ 끊어진 체크포인트 링크: $l"; exit 1; }
done

echo "▶ 대상 $S3"
echo "  원본 루트 $SRC_ROOT → 복원 루트 $EMR_ROOT"
echo
echo "  ⚠ 버킷에 '7일 후 삭제' 수명주기가 걸려 있습니다(모든 접두사)."
echo "    올린 뒤 7일 안에 AMI를 구우세요. 지나면 이 스크립트를 다시 돌리면 됩니다."
echo

# `pack-envs.sh check` 로 전제만 보고 끝낸다. 51GB 업로드는 몇 시간짜리라,
# 두 시간 올린 뒤에 "체크포인트 링크가 끊겨 있습니다"를 보는 건 너무 비싸다.
# 위 사전 점검이 여기까지 왔다면 올릴 것은 전부 제자리에 있다는 뜻이다.
if [ "${1:-}" = "check" ]; then
  echo "✔ 전제 검사 통과 — 올릴 준비가 됐습니다."
  echo "  실제 업로드: bash $0"
  exit 0
fi

# ── 유틸 ─────────────────────────────────────────────────────────────────
# 이미 올라가 있고 크기가 같으면 건너뛴다. 51GB 업로드가 한 번에 끝나는 일은
# 드물어서(회선이 끊기거나 노트북이 잠든다) 재실행이 싸야 한다.
already() {   # already <s3키> <기대바이트>
  local have
  have=$(aws s3api head-object --bucket "$S3_BUCKET" --key "$PREFIX/$1" \
           --query ContentLength --output text 2>/dev/null) || return 1
  [ "$have" = "$2" ]
}
put() {       # put <로컬파일> <s3키>
  local sz; sz=$(stat -c %s "$1")
  if already "$2" "$sz"; then echo "    (이미 올라가 있음, 건너뜀)"; return 0; fi
  aws s3 cp "$1" "$S3/$2" --only-show-errors
  echo "    올림 $(( sz / 1048576 ))MB"
}
# 만드는 데 몇 분씩 걸리는 조각은 만들기 전에 S3를 먼저 본다.
uploaded() { aws s3api head-object --bucket "$S3_BUCKET" --key "$PREFIX/$1" >/dev/null 2>&1; }

# conda-pack 의 진행바는 터미널이 아니면 \r 대신 줄을 계속 덧붙여서, 로그로
# 리다이렉트하면 수천 줄이 쌓이고 정작 봐야 할 메시지가 묻힌다. 로그로 돌릴
# 때만(= stdout이 터미널이 아닐 때) 조용히 시킨다.
CP_QUIET=""; [ -t 1 ] || CP_QUIET="--quiet"

# ── 1. conda 환경 ────────────────────────────────────────────────────────
# --dest-prefix: 환경 안에 **절대경로가 구워져 있다**(shebang, conda-meta, .pth).
#   복원 경로로 미리 바꿔서 포장한다. 안 주면 EC2에서 /home/<나>/... 를 찾다 죽는다.
# --ignore-editable-packages: detectron2와 pytorch3d가 editable로 깔려 있어 conda-pack이
#   기본적으로 포장을 거부한다. editable이 가리키는 **소스 트리는 환경 밖**이라
#   conda-pack이 경로를 못 바꾼다 — 그 몇 개는 bootstrap-ami.sh가 sed로 고친다.
for e in $ENVS; do
  key="envs/$e.tar.gz"
  if uploaded "$key"; then echo "▶ 환경 $e — 이미 올라가 있음, 건너뜀"; continue; fi
  echo "▶ 환경 $e 포장 (몇 분 걸립니다)"
  # --ignore-missing-files 가 없으면 sam3d 에서 conda-pack 이 거부한다.
  # conda-meta 는 setuptools 82.0.1 을 기록해 뒀는데 pip 가 그 위에 81.0.0 을
  # 덮어써서, conda 가 "있어야 한다"고 아는 파일 89개가 실제로는 없다.
  # 세어 보니 82개는 .pyc(파이썬이 다시 만든다), 7개는 egg-info 메타데이터와
  # Windows 런처 manifest 다. **기능 파일은 세 환경 통틀어 0개**라서 무시해도 된다.
  #
  # 환경을 "고쳐서" 통과시키면 안 된다. conda 기록과 실제가 어긋났다는 건 이
  # 환경을 conda 로 재현할 수 없다는 뜻이고, 그래서 애초에 통째로 옮기는 중이다.
  # setuptools 를 다시 깔면 그 위에 얹힌 pip 패키지들이 어떻게 될지 알 수 없다.
  # 이 플래그는 검사만 건너뛸 뿐, 포장되는 건 디스크에 **실제로 있는 것**이다.
  "$CONDA_PACK" -p "$SRC_ROOT/miniconda3/envs/$e" \
    --dest-prefix "$EMR_ROOT/miniconda3/envs/$e" \
    --ignore-editable-packages --ignore-missing-files $CP_QUIET \
    --format tar.gz --compress-level 4 --n-threads -1 \
    --output "$STAGE/$e.tar.gz" --force
  put "$STAGE/$e.tar.gz" "$key"
  rm -f "$STAGE/$e.tar.gz"   # 다음 조각을 위해 바로 비운다(위 vhdx 주석 참고)
done

# ── 2. 소스 트리 ─────────────────────────────────────────────────────────
# checkpoints/ 를 **빼지 않는다**. 처음엔 용량 때문에 뺐는데, 거기에 손으로 고친
# pipeline.yaml(파이프라인 설정의 핵심)과 12.3GB 가중치로 가는 심링크 7개,
# 그리고 0바이트로 비워 둔 ss_encoder 두 개가 들어 있다. 빼면 조용히 안 돌아간다.
# 대신 그 안의 **실제 대용량 파일 3개만** 이름으로 뺀다(따로 올린다. 중간에
# 끊겼을 때 339MB tar를 다시 보내는 것과 2.9GB를 다시 보내는 건 다르다).
#
#   .git           — 워커는 이력을 안 쓴다. sam-3d-objects만 269MB다.
#   *.bak* *.experiment — 손으로 실험하다 남은 사본이다. 전에 .bak 파일이 저장소에
#                  섞여 들어가 혼란을 준 적이 있다. AMI에는 넣지 않는다.
echo "▶ 소스 트리 5개 포장"
if uploaded "sources.tar.gz"; then echo "    (이미 올라가 있음, 건너뜀)"; else
  tar --exclude=.git --exclude=__pycache__ --exclude='*.pyc' \
      --exclude='*.bak*' --exclude='*.experiment' \
      --exclude=depth_pro.pt --exclude=best_mp3d.pth --exclude=cubercnn_DLA34_FPN.pth \
      -czf "$STAGE/sources.tar.gz" -C "$SRC_ROOT" $SRC_REPOS
  put "$STAGE/sources.tar.gz" "sources.tar.gz"
  rm -f "$STAGE/sources.tar.gz"
fi

# ── 3. 체크포인트 ────────────────────────────────────────────────────────
# tar로 묶지 않는다. 이미 압축된 바이너리라 묶어도 안 작아지고, 따로 두면 중간에
# 끊겼을 때 이미 받은 파일은 건너뛸 수 있다. 복원 위치는 매니페스트에 적는다.
echo "▶ 체크포인트 3개"
while read -r c; do
  echo "  $c"
  put "$SRC_ROOT/$c" "ckpt/$(basename "$c")"
done <<< "$CKPTS"

# ── 4. HF 캐시 ───────────────────────────────────────────────────────────
# 압축하지 않는다(-c만). safetensors/bin은 이미 압축돼 있어서 gzip을 걸면 28GB를
# CPU로 갈아 넣고 1%도 못 줄인다. 심링크는 **따라가지 않는다**(-h 안 씀) —
# blobs/ 와 snapshots/ 의 하드링크·심링크 구조가 그대로 보존돼야 한다.
# token 파일은 뺀다. 인스턴스에 HF 토큰이 있을 이유가 없고, AMI를 공유하는 순간
# 같이 나간다.
echo "▶ HF 캐시 (27.8GB, 압축 안 함 — 몇 분 걸립니다)"
if uploaded "hfcache.tar"; then echo "    (이미 올라가 있음, 건너뜀)"; else
  tar --exclude=token -cf "$STAGE/hfcache.tar" -C "$SRC_ROOT/.cache" huggingface
  put "$STAGE/hfcache.tar" "hfcache.tar"
  rm -f "$STAGE/hfcache.tar"
fi

# ── 5. torch hub 캐시 ────────────────────────────────────────────────────
# ZoeDepth / DINOv2 / PerspectiveFields / resnet50. 코드가 첫 실행에 자동으로
# 받지만, 그 다운로드가 추론 중에 터지면 작업이 실패하고 GPU 시간만 버린다.
echo "▶ torch hub 캐시"
if uploaded "torchhub.tar"; then echo "    (이미 올라가 있음, 건너뜀)"; else
  tar -cf "$STAGE/torchhub.tar" -C "$SRC_ROOT/.cache" torch
  put "$STAGE/torchhub.tar" "torchhub.tar"
  rm -f "$STAGE/torchhub.tar"
fi

# ── 6. 매니페스트 ────────────────────────────────────────────────────────
# 마지막에 올린다. 부트스트랩은 이걸 **제일 먼저** 읽으므로, 이 파일이 있다는 건
# 앞의 모든 조각이 다 올라갔다는 뜻이 된다(완료 표시 역할).
echo "▶ 매니페스트 생성"
python3 - "$SRC_ROOT" "$EMR_ROOT" "$STAGE/manifest.json" <<'PY'
import json, os, subprocess, sys
src_root, emr_root, out = sys.argv[1:4]

def sha(p):
    r = subprocess.run(["git", "-C", p, "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None

repos = ["sam-3d-objects", "uLayout", "omni3d", "detectron2", "pytorch3d_omni3d_build"]

# HF 저장소 목록은 **출처 기록용**이다(캐시를 그대로 올리므로 복원에는 안 쓴다).
# 나중에 "이 AMI에 어느 리비전이 들었나"를 물을 때 이것만 보면 된다.
hub = os.path.join(src_root, ".cache/huggingface/hub")
hf = []
for d in sorted(os.listdir(hub)) if os.path.isdir(hub) else []:
    if not d.startswith("models--"):
        continue
    ref = os.path.join(hub, d, "refs/main")
    hf.append({
        "repo": d[len("models--"):].replace("--", "/"),
        "revision": open(ref).read().strip() if os.path.isfile(ref) else None,
    })

json.dump({
    # 부트스트랩이 환경 안에 남은 구경로를 찾아 바꿀 때 쓴다.
    "src_root": src_root,
    "emr_root": emr_root,
    "envs": ["sam3d", "uLayout", "omni3d"],
    # 올린 체크포인트를 어디에 되돌려 놓을지. 파일명 → 복원 상대경로.
    "ckpt_dest": {
        "depth_pro.pt": "sam-3d-objects/checkpoints/depth_pro/depth_pro.pt",
        "best_mp3d.pth": "uLayout/ckpt/best_mp3d.pth",
        "cubercnn_DLA34_FPN.pth": "omni3d/checkpoints/indoor/cubercnn_DLA34_FPN.pth",
    },
    "source_commits": {r: sha(os.path.join(src_root, r)) for r in repos},
    "hf": hf,
}, open(out, "w"), indent=2, ensure_ascii=False)
print(f"  소스 {len(repos)}개 / HF 저장소 {len(hf)}개 기록")
PY
aws s3 cp "$STAGE/manifest.json" "$S3/manifest.json" --only-show-errors
rm -f "$STAGE/manifest.json"

echo
echo "✔ 업로드 완료 — $S3"
aws s3 ls "$S3/" --recursive --human-readable --summarize | tail -20
echo
echo "  다음: AMI 원본 인스턴스를 띄우고 그 안에서"
echo "    sudo EMR_AMI_PREFIX=$PREFIX bash bootstrap-ami.sh"
