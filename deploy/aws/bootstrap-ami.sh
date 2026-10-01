#!/bin/bash
# **AMI 원본 인스턴스 안에서** 한 번 돈다. pack-envs.sh가 S3에 올려둔 것들을
# $EMR_ROOT 에 복원한다. 끝나면 bake-ami.sh → verify-ami.sh 순서로 이어간다.
#
#   sudo EMR_AMI_PREFIX=ami/v1 bash bootstrap-ami.sh
#
# 기반 AMI에 필요한 것 — NVIDIA 드라이버, 도커, nvidia-container-toolkit.
# AWS의 "Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)" 가 셋을
# 다 갖고 있다. 프레임워크가 들어간 일반 DLAMI는 쓰지 않는다 — PyTorch가 이미
# 깔려 있어도 우리는 conda 환경을 따로 쓰므로 수십 GB를 그냥 들고 다니는 셈이고,
# 그만큼 스냅샷 요금과 부팅 워밍 시간이 늘어난다.
#
# 빌더 인스턴스의 루트 볼륨은 **150GB로 띄운다**(= config.sh의 EMR_VOLUME_GB).
# AMI 스냅샷 크기가 빌더의 볼륨 크기로 굳고, 그게 EMR_VOLUME_GB의 하한이 된다.
# 더 크게 띄우면 20-launch-template.sh 가 "스냅샷보다 작은 볼륨" 이라고 거부한다.
#
# tarball을 디스크에 떨어뜨리지 않고 S3에서 바로 tar로 흘려보낸다(파이프). 받아서
# 풀면 28GB짜리 HF tar 때문에 피크가 그만큼 더 커진다 — 150GB에서 그 여유는 아깝다.
# 대신 중간에 끊기면 그 조각은 처음부터 다시 받는다(조각 단위 재시도는 아래 참고).
set -euo pipefail

PREFIX=${EMR_AMI_PREFIX:-ami/v1}

cd "$(dirname "$0")"
. ./config.sh
S3=s3://$S3_BUCKET/$PREFIX

echo "▶ 복원 대상 $EMR_ROOT ← $S3"

# ── 0. 사전 점검 ─────────────────────────────────────────────────────────
[ "$(id -u)" = "0" ] || { echo "✗ root로 실행하세요: sudo bash $0"; exit 1; }

for c in aws docker nvidia-smi tar; do
  command -v "$c" >/dev/null || { echo "✗ $c 가 없습니다 — 기반 AMI를 확인하세요"; exit 1; }
done

# 인스턴스 역할로 S3를 읽는다. 정적 키를 AMI에 넣지 않는 이유는 README의
# 보안 절과 같다 — AMI를 공유하는 순간 키가 같이 나간다.
aws sts get-caller-identity >/dev/null 2>&1 || {
  echo "✗ AWS 자격증명이 없습니다 — 인스턴스에 emr 역할을 붙여 띄웠는지 확인하세요"; exit 1; }

# 드라이버가 실제로 GPU를 보는지. 여기서 안 보이면 컨테이너에서도 안 보이고,
# 그건 AMI를 다 구운 뒤 첫 추론에서야 드러난다.
nvidia-smi -L | grep -q GPU || { echo "✗ nvidia-smi 가 GPU를 못 찾습니다"; exit 1; }

# 도커가 GPU를 넘길 수 있는지(nvidia-container-toolkit). 이게 빠진 기반 AMI를
# 쓰면 컨테이너는 뜨는데 torch.cuda.is_available() 이 False가 된다.
docker info 2>/dev/null | grep -q nvidia || {
  echo "✗ 도커에 nvidia 런타임이 없습니다 — nvidia-container-toolkit을 설치하세요"; exit 1; }

# 내용물 약 85GB. 여유 90GB를 요구한다(스트리밍이라 tarball 공간은 안 센다).
#
# 재개 실행에서는 이미 받아둔 조각을 다시 받지 않는다. 그런데 그건 **이미 디스크를
# 쓰고 있으므로** df 여유에서는 빠져 있다. 여유만 보고 막으면 재개가 영원히
# 불가능해진다 — 실제로 73GB를 복원해 둔 빌더가 "여유 23GB"로 거부당했다.
# 그래서 복원해 둔 양을 더해서 "이 볼륨이 전부 담을 수 있나"를 본다.
FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
# `[ -d ... ] && HAVE_GB=...` 로 쓰면 안 된다. 최초 실행에는 $EMR_ROOT 가 없어서
# 이 AND-OR 목록이 1을 돌려주고 set -e 가 거기서 스크립트를 끝낸다 (05-preflight.sh
# 에서 겪은 것과 같은 함정이다).
HAVE_GB=0
if [ -d "$EMR_ROOT" ]; then
  HAVE_GB=$(du -sxBG "$EMR_ROOT" 2>/dev/null | cut -f1 | tr -dc 0-9 || true)
  HAVE_GB=${HAVE_GB:-0}
fi
echo "  루트 여유 ${FREE_GB}GB + 복원분 ${HAVE_GB}GB = $((FREE_GB + HAVE_GB))GB"
[ "$((FREE_GB + HAVE_GB))" -ge 90 ] || {
  echo "✗ 여유 ${FREE_GB}GB — 복원분을 합쳐도 90GB가 안 됩니다. 루트 볼륨을 150GB로 띄우세요."
  exit 1; }

# ── 1. 매니페스트 ────────────────────────────────────────────────────────
# pack-envs.sh가 **마지막에** 올리는 파일이다. 있다는 건 앞 조각이 다 올라갔다는 뜻.
# 없으면 업로드가 덜 끝난 것이므로, 반쯤 복원해놓고 헤매지 않고 여기서 멈춘다.
MAN=/tmp/emr-manifest.json
aws s3 cp "$S3/manifest.json" "$MAN" --only-show-errors || {
  echo "✗ $S3/manifest.json 이 없습니다 — pack-envs.sh가 아직 안 끝났거나,"
  echo "  버킷의 7일 수명주기로 지워졌습니다. 개발 PC에서 pack-envs.sh를 다시 돌리세요."
  exit 1; }

jq_() { python3 -c "import json,sys;print($1)" < "$MAN"; }
SRC_ROOT=$(jq_ 'json.load(sys.stdin)["src_root"]')
MAN_ROOT=$(jq_ 'json.load(sys.stdin)["emr_root"]')
ENVS=$(jq_ '" ".join(json.load(sys.stdin)["envs"])')

# 포장할 때 구워 넣은 복원 경로와 지금 복원하려는 경로가 다르면, 환경 안의
# 절대경로가 전부 어긋난다. conda-pack --dest-prefix 는 포장 시점에 고정된다.
[ "$MAN_ROOT" = "$EMR_ROOT" ] || {
  echo "✗ 매니페스트는 $MAN_ROOT 로 포장됐는데 지금 설정은 $EMR_ROOT 입니다."
  echo "  EMR_ROOT=$MAN_ROOT 로 맞추거나, 개발 PC에서 다시 포장하세요."; exit 1; }
echo "  원본 $SRC_ROOT → 복원 $EMR_ROOT / 환경: $ENVS"

install -d -o 1000 -g 1000 "$EMR_ROOT" "$EMR_ROOT/.cache"

# 완료 표식. 조각이 **끝까지** 복원된 것만 여기에 파일로 남는다.
DONE=$EMR_ROOT/.bootstrap-done
install -d "$DONE"

# S3에서 바로 tar로 흘린다. 이미 끝난 조각은 건너뛰므로 재실행이 싸다.
#
# "이미 있나"를 디렉터리 존재로 판단하면 안 된다. tar는 **첫 파일을 풀 때 이미**
# 디렉터리를 만들어 둔다. 그래서 스트림이 중간에 끊긴 뒤 다시 돌리면 반쯤 풀린
# 조각을 완성된 것으로 보고 건너뛰고, 그렇게 구운 AMI는 멀쩡히 부팅한 뒤 첫
# 추론에서 죽는다 — 제일 찾기 어려운 실패다. 스팟 빌더에서는 더 중요하다:
# SSH가 끊기거나(노트북 절전) 네트워크가 한 번 튀면 바로 이 상황이 된다.
#
# 그래서 tar가 종료코드 0으로 끝난 뒤에만 표식을 남기고, 판단은 표식으로만 한다.
# 다시 풀어도 tar가 같은 파일을 전부 덮어쓰므로 중간에 끊긴 조각 위에 그대로
# 덮어쓰면 된다(환경만 예외적으로 먼저 지운다 — 아래).
#
# set -o pipefail 이 켜져 있어야 `aws s3 cp` 실패가 파이프 밖으로 나온다.
# 안 그러면 tar가 "빈 입력"을 받고 0으로 끝나서 표식까지 남는다. 머리말의
# `set -euo pipefail` 을 지우지 말 것.
stream() {   # stream <표식이름> <s3키> <tar옵션> <풀 위치>
  if [ -f "$DONE/$1" ]; then echo "▶ $1 — 이미 복원됨, 건너뜀"; return 0; fi
  echo "▶ $1 복원 — 내려받아 푸는 중: $2"
  aws s3 cp "$S3/$2" - | tar -x"$3"f - -C "$4"
  : > "$DONE/$1"
}

# ── 2. conda 환경 ────────────────────────────────────────────────────────
# conda-pack 산출물은 **최상위 디렉터리가 없다**(내용이 바로 루트에 있다).
# 그래서 환경 디렉터리를 먼저 만들고 그 안으로 푼다.
for e in $ENVS; do
  d=$EMR_ROOT/miniconda3/envs/$e
  if [ -f "$DONE/env-$e" ]; then echo "▶ 환경 $e — 이미 복원됨, 건너뜀"; continue; fi
  # 환경만 먼저 지운다. 앞서 끊긴 시도가 남긴 파일이 섞이면 conda 환경은
  # import 단계에서 어긋나고, 그 원인을 뒤에서 찾기가 매우 어렵다.
  rm -rf "$d"; install -d "$d"
  stream "env-$e" "envs/$e.tar.gz" z "$d"
  # --dest-prefix 로 포장했으면 경로가 이미 최종값이라 할 일이 없지만, 버전에
  # 따라 conda-unpack 이 같이 들어오기도 한다. 있으면 돌려서 손해 볼 게 없다.
  [ -x "$d/bin/conda-unpack" ] && "$d/bin/conda-unpack" || true
done

# ── 3. 소스 트리 ─────────────────────────────────────────────────────────
stream "sources" "sources.tar.gz" z "$EMR_ROOT"

# ── 4. 체크포인트 ────────────────────────────────────────────────────────
# 소스 tar에서 이름으로 빼둔 대용량 3개를 제자리에 돌려놓는다.
echo "▶ 체크포인트 3개"
python3 -c "import json,sys;[print(k,v) for k,v in json.load(sys.stdin)['ckpt_dest'].items()]" < "$MAN" |
while read -r name dest; do
  full=$EMR_ROOT/$dest
  if [ -f "$DONE/ckpt-$name" ]; then echo "  $name — 이미 있음"; continue; fi
  install -d "$(dirname "$full")"
  # .part 로 받고 끝난 뒤에 옮긴다. 바로 받으면 중간에 끊긴 파일도 비어 있지는
  # 않아서 "이미 있음"으로 통과하고, 가중치가 잘린 채 AMI에 들어간다.
  aws s3 cp "$S3/ckpt/$name" "$full.part" --only-show-errors
  mv -f "$full.part" "$full"
  : > "$DONE/ckpt-$name"
  echo "  $name → $dest"
done

# ── 5. 캐시 ──────────────────────────────────────────────────────────────
# HF 캐시는 blobs/snapshots 의 심링크·하드링크 구조가 그대로 보존돼야 한다.
# sam-3d-objects/checkpoints/hf/checkpoints 의 체크포인트 7개가 blob 파일명으로
# 걸린 **상대** 심링크이기 때문이다(`../../../../.cache/...`). 상대 경로라서
# 루트가 바뀌어도 따라오지만, blobs 안의 파일명이 하나라도 달라지면 끊긴다.
# 그래서 다시 받지 않고 캐시를 통째로 옮긴다.
stream "hfcache" "hfcache.tar" "" "$EMR_ROOT/.cache"   # 28GB, 몇 분 걸린다
stream "torchcache" "torchhub.tar" "" "$EMR_ROOT/.cache"

# ── 6. 남은 구경로 정리 ──────────────────────────────────────────────────
# conda-pack --dest-prefix 는 **환경 안의** 경로만 바꾼다. detectron2와 pytorch3d는
# editable로 깔려 있어서 환경 밖 소스 트리를 가리키는데(`__editable___*_finder.py`,
# `*.pth`), 그 경로는 conda-pack이 손댈 수 없다. 여기서 바꾼다.
#
# -I 로 **텍스트 파일만** 고른다. 바이너리는 sed로 못 고친다 — 길이가 달라지면
# 파일이 깨진다. 개발 PC에서는 sam3d 697 / uLayout 192 / omni3d 335개가 걸렸다.
# 바이너리에 남은 구경로(.pyc, .so)는 아래 6a / 6b 에서 종류별로 따로 처리한다.
echo "▶ 환경 안에 남은 구경로 치환 ($SRC_ROOT → $EMR_ROOT)"
for e in $ENVS; do
  d=$EMR_ROOT/miniconda3/envs/$e
  # `|| true` 를 빼면 안 된다. 재실행처럼 **고칠 게 하나도 없을 때** grep이 1을
  # 돌려주고, set -o pipefail 이 그걸 파이프 밖으로 내보내고, set -e 가 아무
  # 메시지도 없이 스크립트를 끝낸다. 성공한 재실행이 침묵 속에 죽는 꼴이다.
  n=$(grep -rlI -- "$SRC_ROOT" "$d" 2>/dev/null | wc -l || true)
  if [ "$n" -gt 0 ]; then
    grep -rlI -- "$SRC_ROOT" "$d" 2>/dev/null \
      | xargs -r -d '\n' sed -i "s|$SRC_ROOT|$EMR_ROOT|g"
  fi
  echo "  $e: ${n}개 파일 치환"
done

# 위 sed는 텍스트 파일만 고쳤다. 남은 구경로는 두 종류이고, 성격이 전혀 다르다.
#
#  (1) .pyc — 컴파일 당시의 소스 경로가 co_filename 에 박힌다. 이 빌더에서 세어
#      보니 48,858개였다. 그런데 파이썬은 import를 **파일시스템 경로**로 찾고
#      .pyc 가 최신인지도 원본의 mtime/size 로 판단한다 — 박힌 경로는 트레이스백
#      표시에만 쓰인다. 즉 실행에는 무해하다.
#      그래도 그냥 두지 않는 이유: 에러 메시지가 존재하지 않는 경로를 가리키면
#      디버깅이 괴롭다. 반대로 **지우기만 하면** 인스턴스가 뜰 때마다 약 49,000개를
#      다시 컴파일해서 콜드스타트가 느려진다. 그래서 여기서 지우고 다시 만든다.
#
#  (2) ELF(.so) — 동적 로더가 읽는 RPATH/RUNPATH 에 구경로가 있으면 진짜 문제다.
#      sed로는 못 고치고(길이가 바뀌면 파일이 깨진다) patchelf 로 헤더를 다시 쓴다.
#      이 빌더에서는 3개였다: pytorch3d/_C.so, _nvdiffrast_c.so, libc10_stub.so —
#      전부 editable 설치라 conda-pack 이 손대지 못한 것들이다.
#
# **게이트를 "문자열이 있나"로 만들면 안 된다.** ELF의 .rodata 에는 빌드 당시
# 경로가 그대로 남는다(PyTorch의 "INTERNAL ASSERT FAILED at ..." 메시지. _C.so
# 한 파일에만 668군데). 이건 지울 수도 없고 지울 필요도 없다. 예전 게이트가
# `grep -rl` 로 문자열 유무만 봐서 **영원히 통과할 수 없었고**, 실제로 여기서
# "바이너리 48861개" 라며 멈췄다. 그래서 아래는 RPATH 값을 본다.

# ── 6a. .pyc 재컴파일 ────────────────────────────────────────────────────
echo "▶ .pyc 재컴파일 (바이트코드에 박힌 구경로)"
for e in $ENVS; do
  d=$EMR_ROOT/miniconda3/envs/$e
  find "$d" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
  # compileall 은 **하나라도** 문법 오류가 나면 1을 돌려준다. 환경 안에는 py2
  # 시절 테스트 픽스처처럼 원래부터 컴파일 안 되는 파일이 섞여 있다(세 환경 모두
  # 그랬다). 그걸로 부트스트랩 전체를 멈추면 안 되므로 상태를 삼킨다.
  "$d/bin/python" -m compileall -q -j 0 "$d/lib" >/dev/null 2>&1 || true
  echo "  $e: 완료"
done

# ── 6b. ELF RPATH 치환 ───────────────────────────────────────────────────
# patchelf 는 기반 AMI에 없다. apt 목록이 오래돼 있어서 update 부터 해야 받아진다.
if ! command -v patchelf >/dev/null; then
  echo "▶ patchelf 설치"
  apt-get update -qq >/dev/null 2>&1 || true
  apt-get install -y -qq patchelf >/dev/null 2>&1 || true
fi
if ! command -v patchelf >/dev/null; then
  # apt 미러가 막혀 있어도 되게 한 번 더. pip 휠 안에 바이너리가 들어 있다.
  FIRST_ENV=$(echo "$ENVS" | awk '{print $1}')
  "$EMR_ROOT/miniconda3/envs/$FIRST_ENV/bin/python" -m pip install \
    --quiet --user --root-user-action=ignore patchelf >/dev/null 2>&1 || true
  export PATH="$PATH:/root/.local/bin"
fi
command -v patchelf >/dev/null || {
  echo "✗ patchelf 를 구하지 못했습니다 — RPATH를 고칠 수 없습니다"; exit 1; }

# 환경 전체를 `file` 로 훑으면 50만 개라 너무 느리다. ELF 가 될 수 있는 건
# 공유 라이브러리와 bin/ 안의 실행파일뿐이다(4,076개 → 약 1분40초).
scan_elf() {
  find "$EMR_ROOT/miniconda3/envs" -type f \
       \( -name '*.so' -o -name '*.so.*' -o -path '*/bin/*' \) -print0 2>/dev/null \
  | while IFS= read -r -d '' f; do
      rp=$(patchelf --print-rpath "$f" 2>/dev/null) || continue   # ELF 아니면 건너뜀
      case "$rp" in *"$SRC_ROOT"*) printf '%s\n' "$f";; esac
    done
}

echo "▶ ELF RPATH 치환"
ELF_N=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  old=$(patchelf --print-rpath "$f")
  patchelf --set-rpath "${old//$SRC_ROOT/$EMR_ROOT}" "$f"
  ELF_N=$((ELF_N+1))
done <<< "$(scan_elf || true)"
echo "  ${ELF_N}개 치환"

# ── 6c. 게이트 ───────────────────────────────────────────────────────────
# 여기를 통과 못 하면 AMI를 굽지 않는다. 다 굽고 나서 첫 요청에 죽는 것보다
# 지금 멈추는 쪽이 싸다.
GATE_FAIL=0
LEFT_TXT=$(grep -rlI --exclude-dir=__pycache__ -- "$SRC_ROOT" "$EMR_ROOT/miniconda3/envs" 2>/dev/null || true)
if [ -n "$LEFT_TXT" ]; then
  echo "✗ 텍스트 파일에 구경로가 남았습니다 (sed가 놓쳤다):"
  echo "$LEFT_TXT" | head
  GATE_FAIL=1
fi
LEFT_ELF=$(scan_elf || true)
if [ -n "$LEFT_ELF" ]; then
  echo "✗ ELF RPATH 에 구경로가 남았습니다:"
  echo "$LEFT_ELF" | head
  GATE_FAIL=1
fi
[ "$GATE_FAIL" = "0" ] || exit 1
echo "  ✓ 구경로 없음 — 텍스트 / RPATH 둘 다 깨끗"

# ── 7. 저장소 ────────────────────────────────────────────────────────────
# bake-ami.sh 는 저장소가 $EMR_REPO_DIR 에 있기를 요구한다.
if [ -d "$EMR_REPO_DIR/.git" ]; then
  echo "▶ 저장소 — 이미 있음"
else
  echo "▶ 저장소 clone"
  git clone "${EMR_GIT_URL:-https://github.com/simu1231/Empty_My_Room.git}" "$EMR_REPO_DIR"
  [ -n "${EMR_GIT_REF:-}" ] && git -C "$EMR_REPO_DIR" checkout -q "$EMR_GIT_REF"
fi

# clone한 코드가 /opt/emr 경로 변경을 포함하는지 본다. 옛 커밋에는 Dockerfile에
# 홈 경로가 박혀 있어서, 그걸로 이미지를 구우면 컨테이너가 뜨고 나서 첫 요청에
# ModuleNotFoundError 로 죽는다 — AMI를 다 구운 뒤다. 여기서 막는다.
grep -q 'ARG EMR_ROOT' "$EMR_REPO_DIR/deploy/gpu/Dockerfile" || {
  echo "✗ clone한 저장소의 deploy/gpu/Dockerfile 에 'ARG EMR_ROOT' 가 없습니다."
  echo "  경로 변경 커밋이 아직 push되지 않았습니다. 개발 PC에서 push한 뒤 다시 돌리세요"
  echo "  (또는 EMR_GIT_REF=<커밋> 으로 지정)."; exit 1; }

# ── 8. 소유권 ────────────────────────────────────────────────────────────
# 컨테이너는 uid 1000(emr)으로 돈다. HF 캐시는 락 파일을 쓰므로 읽기 권한만으로는
# 부족하다 — root 소유로 두면 첫 추론에서 PermissionError 가 난다.
echo "▶ 소유권 uid 1000 으로 정리"
chown -R 1000:1000 "$EMR_ROOT"

echo
echo "✔ 복원 완료"
du -sh "$EMR_ROOT" 2>/dev/null | awk '{print "  "$2" 사용량: "$1}'
df -h / | tail -1 | awk '{print "  루트 "$3" 사용 / "$4" 남음"}'
echo
echo "  다음: cd $EMR_REPO_DIR/deploy/aws"
# && 로 묶지 않는다. 중간이 실패하면 어디서 멈췄는지 덜 보인다.
echo "        bash bake-ami.sh     # 세 이미지를 git SHA 태그로 빌드"
echo "        bash smoke-test.sh   # 스택을 띄워 세 모델에 진짜 작업을 통과시킨다"
echo "        bash verify-ami.sh   # AMI가 되기 위한 조건 점검"
