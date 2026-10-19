#!/bin/bash
# "이 AMI 가 fal 전환 코드를 담고 있는가"를 **명시적으로** 판정하는 공용 함수.
#
# 왜 AMI ID 비교로는 안 되나: 새 ID 라는 사실은 내용을 말해 주지 않는다. 이번 전환에서 AMI 에 구워지는 것은
# ① 이미지 안의 worker.py / /opt/fal-deps ② 호스트 저장소 체크아웃의 sam3d 백엔드 코드와 compose 파일이다.
# 둘 중 하나라도 옛 것이면 fal userdata 와 짝지어도 워커가 조용히 로컬로 돈다(이미 v19 가 그 상태다).
#
# 방식: AMI 에 태그 `FalReady=<해시>` 를 붙인다. 해시는 fal 관련 파일들의 **git blob id**(HEAD 기준)를 이어 붙인
# sha256 이다. 태그를 붙이는 쪽(verify-ami.sh, 빌더 안)과 확인하는 쪽(20/35 스크립트, 개발 PC)이 같은 목록을
# 같은 방식으로 계산하므로, "AMI 를 굽던 코드 = 지금 배포하려는 코드" 일 때만 일치한다. 이 파일들 중 하나가
# 나중에 바뀌면 해시가 달라져 새 AMI 를 요구한다 — 이미지에 구워진 코드가 바뀐 것이므로 맞는 동작이다.
#
# 한계: 태그는 사람이 붙인다. 그래서 verify-ami.sh 가 빌더 안에서 **이미지 내용을 직접 검사한 뒤에만** 태그가 든
# 명령을 출력한다(check-ami-local). 그 명령 밖에서 손으로 붙인 태그는 이 방식이 막지 못한다.
#
#   source fal-ready.sh              # 함수만 쓴다
#   bash fal-ready.sh hash           # 해시 한 줄
#   bash fal-ready.sh files          # 대상 파일 목록
#   bash fal-ready.sh check-ami-local   # 빌더 안에서 이미지·체크아웃 검사(통과 0 / 실패 1)

# 해시 대상. worker.py·gpu/Dockerfile·requirements 는 이미지에, 나머지는 호스트 체크아웃(마운트)에 들어간다.
FAL_READY_FILES=(
  deploy/worker/worker.py
  deploy/worker/requirements.txt
  deploy/gpu/Dockerfile
  deploy/docker-compose.gpu.yml
  deploy/docker-compose.aws.yml
  sam3d/backend/main.py
  sam3d/backend/routers/sam3d.py
  sam3d/backend/services/sam3d_runner.py
  sam3d/backend/services/sam3d_fal.py
)

# userdata 가 담아야 하는 기능 표식. 표식만으로는 통과시키지 않는다(아래 코드 조각도 함께 본다).
FAL_USERDATA_MARKER='emr-userdata-features: fal-ssm gate-off ami-guard v1'

_fal_repo_top() {
  git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null
}

# 저장소 HEAD 기준 해시(16자). 실패하면 빈 문자열과 비0 을 돌려준다.
fal_ready_hash() {
  local top f id out=""
  top=${EMR_REPO_TOP:-$(_fal_repo_top)} || return 1
  [ -n "$top" ] || return 1
  for f in "${FAL_READY_FILES[@]}"; do
    id=$(git -C "$top" rev-parse "HEAD:$f" 2>/dev/null) || return 1
    [ -n "$id" ] || return 1
    out="$out$id  $f"$'\n'
  done
  printf '%s' "$out" | sha256sum | cut -c1-16
}

# 대상 파일이 커밋과 같은지(작업트리 수정이 없는지). 다르면 목록을 찍고 1.
fal_ready_clean() {
  local top dirty
  top=${EMR_REPO_TOP:-$(_fal_repo_top)} || return 1
  dirty=$(git -C "$top" status --porcelain -- "${FAL_READY_FILES[@]}" 2>/dev/null)
  if [ -n "$dirty" ]; then echo "$dirty"; return 1; fi
}

# userdata(복호화된 텍스트, 표준입력)가 fal 모드이고 이번 안전장치를 전부 담았는지.
# 사유를 표준출력에 한 줄씩 찍고, 하나라도 빠지면 1.
fal_userdata_check() {
  local ud miss=0
  ud=$(cat)
  [ -n "$ud" ] || { echo "userdata 가 비어 있다"; return 1; }
  _need() { printf '%s' "$ud" | grep -qF -- "$1" || { echo "누락: $2"; miss=1; }; }
  _need "$FAL_USERDATA_MARKER"            "기능 표식($FAL_USERDATA_MARKER)"
  _need 'if [ "fal" = fal ]; then'        "fal 모드 렌더(SSM 읽기 구간이 fal 로 켜져 있어야 한다)"
  _need '--with-decryption'               "SSM SecureString 복호화 읽기"
  _need 'SAM3D_BACKEND=fal'               ".env 의 SAM3D_BACKEND=fal 쓰기"
  _need 'WARM_GATE_FILE='                 "워밍 게이트 비활성화(.env 의 WARM_GATE_FILE=)"
  _need '[ "fal" != fal ] ||'             "옛 AMI 차단 가드(워커 컨테이너의 실제 SAM3D_BACKEND 검사)"
  return "$miss"
}

# 빌더 안에서: 이 인스턴스의 이미지와 체크아웃이 fal 전환 코드를 담았는지 직접 본다.
# 통과해야만 verify-ami.sh 가 FalReady 태그가 든 create-image 명령을 출력한다.
fal_check_ami_local() {
  local tag=${EMR_IMAGE_TAG:?EMR_IMAGE_TAG} root=${EMR_REPO_DIR:?EMR_REPO_DIR} bad=0
  _ok()  { echo "  ✔ $*"; }
  _bad() { echo "  ✗ $*"; bad=1; }

  if d=$(EMR_REPO_TOP=$root fal_ready_clean); then _ok "fal 대상 파일이 커밋과 같다"
  else _bad "fal 대상 파일에 커밋 안 된 수정이 있다 — 해시가 이 AMI 의 내용과 어긋난다:"; echo "$d" | sed 's/^/      /'; fi

  docker image inspect "emr/gpu:$tag" >/dev/null 2>&1 \
    || { _bad "이미지 emr/gpu:$tag 가 없다"; return 1; }
  if docker run --rm --entrypoint sh "emr/gpu:$tag" -c \
       'test -d /opt/fal-deps/fal_client && grep -q "run_sam3d" /app/worker.py' >/dev/null 2>&1; then
    _ok "이미지 emr/gpu:$tag 에 /opt/fal-deps/fal_client 와 fal 분기(worker.py run_sam3d)가 있다"
  else
    _bad "이미지 emr/gpu:$tag 가 옛 것이다(/opt/fal-deps 또는 worker.py 의 run_sam3d 없음) — bake-ami.sh 를 새 커밋에서 다시"
  fi
  grep -q 'SAM3D_BACKEND' "$root/deploy/docker-compose.gpu.yml" \
    && _ok "체크아웃의 compose 가 SAM3D_BACKEND 를 컨테이너에 전달한다" \
    || _bad "체크아웃의 docker-compose.gpu.yml 이 옛 것이다(SAM3D_BACKEND 전달 없음)"
  grep -q 'WARM_GATE_FILE: \${WARM_GATE_FILE-' "$root/deploy/docker-compose.aws.yml" \
    && _ok "체크아웃의 docker-compose.aws.yml 이 게이트 비활성화를 지원한다" \
    || _bad "체크아웃의 docker-compose.aws.yml 이 옛 것이다(WARM_GATE_FILE 비활성화 없음)"
  grep -q 'def run_sam3d' "$root/sam3d/backend/services/sam3d_runner.py" \
    && [ -f "$root/sam3d/backend/services/sam3d_fal.py" ] \
    && _ok "체크아웃의 sam3d 백엔드에 run_sam3d / sam3d_fal.py 가 있다" \
    || _bad "체크아웃의 sam3d 백엔드가 옛 것이다"
  return "$bad"
}

case "${BASH_SOURCE[0]}" in "$0")   # 직접 실행했을 때만
  case "${1:-}" in
    hash)  fal_ready_hash || { echo "해시를 계산하지 못했다(git 저장소/HEAD 확인)" >&2; exit 1; } ;;
    files) printf '%s\n' "${FAL_READY_FILES[@]}" ;;
    check-ami-local) fal_check_ami_local ;;
    *) echo "사용법: $0 hash | files | check-ami-local" >&2; exit 2 ;;
  esac ;;
esac
