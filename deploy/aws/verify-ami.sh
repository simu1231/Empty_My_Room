#!/bin/bash
# 스냅샷 찍기 **직전에 인스턴스 안에서** 돌린다. 여기서 막지 못한 문제는
# 전부 "부팅은 됐는데 요금만 나가는" 형태로 나타난다 — 제일 찾기 어렵고
# 제일 비싼 실패다.
#
# 특히 aws CLI. 없으면 가디언이 인스턴스를 회수할 수 없고(shutdown은 대안이
# 아니다 — ASG가 교체 인스턴스를 띄운다), 그 사실은 요금 고지서로만 알게 된다.
set -uo pipefail
cd "$(dirname "$0")"
. ./config.sh

FAIL=0
ok()   { echo "  ✔ $*"; }
bad()  { echo "  ✗ $*"; FAIL=1; }
warn() { echo "  ! $*"; }

echo "▶ 요금을 끊는 데 반드시 필요한 것"
command -v aws >/dev/null 2>&1 \
  && ok "aws CLI $(aws --version 2>&1 | cut -d' ' -f1)" \
  || bad "aws CLI 없음 — 가디언이 인스턴스를 회수할 수 없다"
# /opt/emr/bin 은 755라 일반 사용자로도 확인된다. sudo를 쓰면 비밀번호가
# 필요할 때 명령이 실패하고, 그 실패가 "파일 없음"으로 잘못 읽힌다.
[ -x /opt/emr/bin/self-retire.sh ] \
  && ok "self-retire.sh 설치됨" || bad "/opt/emr/bin/self-retire.sh 없음"
[ -x /opt/emr/bin/guardian.sh ] \
  && ok "guardian.sh 설치됨" || bad "/opt/emr/bin/guardian.sh 없음"
if systemctl is-enabled emr-guardian.timer >/dev/null 2>&1; then
  ok "emr-guardian.timer enabled ($(systemctl is-active emr-guardian.timer))"
else
  bad "emr-guardian.timer 가 enabled 가 아니다 — 재부팅하면 가디언이 안 돈다"
fi

echo "▶ 런타임"
command -v docker >/dev/null 2>&1 && ok "docker $(docker --version | cut -d' ' -f3 | tr -d ,)" \
  || bad "docker 없음"
systemctl is-enabled docker >/dev/null 2>&1 && ok "docker enabled" \
  || bad "docker 가 부팅 시 자동 시작이 아니다"
if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
  ok "nvidia 런타임 등록됨"
else
  bad "nvidia 런타임이 없다 — GPU 컨테이너가 안 뜬다"
fi
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null \
  | sed 's/^/  ✔ GPU /' || bad "nvidia-smi 실패"

echo "▶ 이미지 (태그 $EMR_IMAGE_TAG)"
for img in "emr/api:$EMR_IMAGE_TAG" "emr/worker:$EMR_IMAGE_TAG" "emr/gpu:$EMR_IMAGE_TAG"; do
  if sz=$(docker image inspect "$img" --format '{{.Size}}' 2>/dev/null); then
    ok "$img ($(( sz / 1024 / 1024 )) MB)"
  else
    bad "$img 없음 — bake-ami.sh 를 먼저 돌려야 한다"
  fi
done
# 태그가 서로 같은 이미지를 가리키면 CPU/GPU 빌드가 충돌한 것이다.
W=$(docker image inspect "emr/worker:$EMR_IMAGE_TAG" --format '{{.Id}}' 2>/dev/null || echo w)
G=$(docker image inspect "emr/gpu:$EMR_IMAGE_TAG"    --format '{{.Id}}' 2>/dev/null || echo g)
[ "$W" = "$G" ] && bad "emr/worker 와 emr/gpu 가 같은 이미지다 — 태그가 덮어써졌다" \
                || ok "worker / gpu 이미지가 서로 다르다"

echo "▶ 저장소와 가중치"
[ -d "$EMR_REPO_DIR" ] && ok "$EMR_REPO_DIR 존재" \
  || bad "$EMR_REPO_DIR 없음 — userdata가 cd 하다 죽는다"
for f in docker-compose.yml docker-compose.gpu.yml docker-compose.aws.yml; do
  [ -f "$EMR_REPO_DIR/deploy/$f" ] && ok "deploy/$f" || bad "deploy/$f 없음"
done
for d in $EMR_WARM_DIRS; do
  if [ -d "$d" ]; then ok "$(basename "$d") $(du -sh "$d" 2>/dev/null | cut -f1)"
  else bad "$d 없음 — 모델 가중치가 AMI에 안 들어갔다"; fi
done

echo "▶ 경로 일치 (루트 $EMR_ROOT)"
# 이게 이 스크립트에서 제일 중요한 검사다. 컨테이너는 호스트와 **같은 절대경로**로
# 모델을 마운트하는데, 경로가 하나라도 없으면 도커는 에러를 내지 않고 빈 디렉터리를
# root 소유로 만들어 준다. 그러면 스택은 멀쩡히 뜨고 리퍼도 뜨고 ASG도 정상으로
# 보다가, 첫 요청에서 ModuleNotFoundError로 죽는다 — 그때까지 요금은 계속 나간다.
# sam2_repo / lama_repo / lama_model 은 backend 컨테이너가 2단계에 쓴다.
# 이 셋은 AMI 에 늦게 들어왔고(백엔드를 인스턴스에 올린 커밋), 빠지면 2단계
# 첫 클릭에서 죽는다. userdata 에 부팅시 검사가 있지만 그건 다 구운 뒤라
# 걸려도 베이크를 다시 해야 한다 — 스냅샷 전에 여기서 잡는다.
for d in miniconda3 sam-3d-objects uLayout omni3d detectron2 \
         pytorch3d_omni3d_build sam2_repo lama_repo lama_model \
         .cache/huggingface .cache/torch; do
  [ -d "$EMR_ROOT/$d" ] && ok "$EMR_ROOT/$d" \
    || bad "$EMR_ROOT/$d 없음 — 도커가 빈 디렉터리로 때워서 첫 요청에서 죽는다"
done
[ -d "$EMR_REPO_DIR/sam3d/backend" ] && ok "$EMR_REPO_DIR/sam3d/backend" \
  || bad "$EMR_REPO_DIR/sam3d/backend 없음 (compose 마운트 대상)"

# 이미지에 구워진 EMR_ROOT 가 지금 설정과 같은지. 다르면 컨테이너 안의 python
# 경로가 호스트 마운트와 어긋나 역시 첫 요청에서 죽는다.
BAKED=$(docker image inspect "emr/gpu:$EMR_IMAGE_TAG" \
          --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | sed -n 's/^EMR_ROOT=//p')
if [ -z "$BAKED" ]; then
  bad "emr/gpu 이미지에 EMR_ROOT 가 없다 — 옛 Dockerfile로 구운 이미지다"
elif [ "$BAKED" != "$EMR_ROOT" ]; then
  bad "이미지에 구워진 EMR_ROOT=$BAKED 인데 설정은 $EMR_ROOT 다 — 다시 빌드해야 한다"
else
  ok "이미지에 구워진 EMR_ROOT 가 설정과 같다"
fi

# SAM3D 가중치 12.3GB로 가는 길. 파이프라인은 pipeline.yaml 과 같은 디렉터리의
# *.ckpt / *.pt 를 읽는데, 그게 HF 캐시의 blob 파일명(sha256)으로 걸린 **상대**
# 심링크 7개다. 캐시를 통째로 옮기지 않고 새로 받으면 blob 이름이 달라져 여기가
# 끊기고, 스택은 멀쩡히 뜬 뒤 첫 추론에서 죽는다. 굽기 전에 잡을 마지막 지점이다.
HFCK=$EMR_ROOT/sam-3d-objects/checkpoints/hf/checkpoints
if [ ! -f "$HFCK/pipeline.yaml" ]; then
  bad "$HFCK/pipeline.yaml 없음 — SAM3D 파이프라인 설정이 빠졌다"
else
  _n=0; _broken=""
  for l in "$HFCK"/*.ckpt "$HFCK"/*.pt; do
    [ -e "$l" ] && _n=$((_n+1)) || _broken="$_broken $(basename "$l")"
  done
  if [ -n "$_broken" ]; then
    bad "끊어진 체크포인트 링크:$_broken — HF blob 이름이 어긋났다"
  else
    ok "SAM3D 체크포인트 링크 ${_n}개 모두 연결됨"
  fi
  unset _n _broken
fi

# SAM2(857MB) / LaMa(391MB). 위의 심링크 검사와 달리 이 둘은 S3 에서 **파일로**
# 따로 내려와 매니페스트의 ckpt_dest 가 정한 자리에 놓인다. 디렉터리 존재만
# 보면 안 되는 이유 — 내려받다 끊기면 빈 파일이나 잘린 파일이 그 자리에 남고,
# 스택은 멀쩡히 뜬 뒤 2단계 첫 클릭에서 죽는다. 그래서 크기 하한으로 본다.
while read -r _f _min _label; do
  if [ ! -f "$_f" ]; then
    bad "$_label 없음 — $_f (2단계가 통째로 죽는다)"
  else
    _sz=$(stat -c %s "$_f" 2>/dev/null || echo 0)
    if [ "$_sz" -lt "$_min" ]; then
      bad "$_label 가 잘렸다 — $((_sz/1024/1024))MB (최소 $((_min/1024/1024))MB)"
    else
      ok "$_label $((_sz/1024/1024))MB"
    fi
  fi
done <<CKPT2
$EMR_ROOT/sam2_repo/checkpoints/sam2.1_hiera_large.pt 838860800 SAM2 체크포인트
$EMR_ROOT/lama_model/big-lama/models/best.ckpt 367001600 LaMa 체크포인트
CKPT2
unset _f _min _label _sz

# 위까지는 체크포인트 **파일**이 제자리에 있는지만 봤다. 파일이 있어도 코드가
# 다른 곳을 보고 있으면 똑같이 첫 추론에서 죽는다. 실제로 그랬다 —
# sam3d_runner.CKPT_DIR 의 기본값에 개발 PC의 홈 경로가 박혀 있었고,
# SAM3D_CKPT_DIR 을 설정하는 곳은 저장소 어디에도 없었다. 즉 EC2에서는 **항상**
# 그 폴백이 쓰였다. 게다가 워커는 작업을 받을 때까지 load_pipeline() 을 부르지
# 않으므로 컨테이너는 정상으로 뜨고 리퍼도 돌고 ASG 헬스체크도 통과한다.
# 어긋남은 **첫 요청에서야** 드러난다 — 이 스크립트 머리말이 말하는 그 실패다.
#
# 그래서 경로를 여기서 다시 계산하지 않는다(그러면 같은 버그를 복사하는 꼴이다).
# AMI에 들어간 conda 환경의 python 으로 모듈을 **실제로 import 해서** 코드가
# 해석한 값을 받아온다. 컨테이너도 같은 prefix 의 같은 env 를 쓰므로 결과가 같다.
PYBIN=$EMR_ROOT/miniconda3/envs/sam3d/bin/python
if [ ! -x "$PYBIN" ]; then
  bad "$PYBIN 없음 — sam3d conda 환경이 안 풀렸다"
else
  _err=$(mktemp)
  CODE_CKPT=$(EMR_ROOT="$EMR_ROOT" PYTHONPATH="$EMR_REPO_DIR/sam3d/backend" \
    "$PYBIN" -c 'from services import sam3d_runner as r; print("CKPT_DIR="+r.CKPT_DIR)' \
    2>"$_err" | sed -n 's/^CKPT_DIR=//p')
  if [ -z "$CODE_CKPT" ]; then
    bad "sam3d_runner 를 import 하지 못했다: $(tail -1 "$_err")"
  elif [ "$CODE_CKPT" != "$HFCK" ]; then
    # 개인 홈 경로가 그대로 남아 있으면 여기서 걸린다.
    bad "코드가 보는 CKPT_DIR=$CODE_CKPT 인데 가중치는 $HFCK 에 있다 — 첫 추론에서 죽는다"
  elif [ ! -f "$CODE_CKPT/pipeline.yaml" ]; then
    bad "코드가 보는 $CODE_CKPT 에 pipeline.yaml 이 없다"
  else
    ok "코드가 보는 CKPT_DIR 이 $EMR_ROOT 아래 가중치와 같다"
  fi
  rm -f "$_err"; unset _err
fi

# 컨테이너는 uid 1000으로 돈다. HF 캐시는 락 파일을 쓰므로 쓰기 권한이 필요하다.
HF_UID=$(stat -c %u "$EMR_ROOT/.cache/huggingface" 2>/dev/null || echo "?")
[ "$HF_UID" = "1000" ] && ok "HF 캐시 소유자 uid 1000" \
  || bad "HF 캐시 소유자가 uid $HF_UID — 첫 추론에서 PermissionError (sudo chown -R 1000:1000 $EMR_ROOT)"

echo "▶ 위생"
[ -f "$EMR_REPO_DIR/deploy/.env" ] \
  && warn ".env 가 남아있다 — userdata가 덮어쓰지만 AMI 공유 시 새어나간다" \
  || ok ".env 없음(부팅 때 생성된다)"
# 디렉터리 **존재**로 판정하면 안 된다. config.sh 가 계정 번호를 얻으려고
# `aws sts get-caller-identity` 를 부르고, CLI v2 는 그때마다 ~/.aws/cli/cache/
# session.db 를 쓴다. 그런데 이 스크립트 자신이 맨 위에서 config.sh 를 읽는다 —
# 즉 자기가 만든 파일을 자기가 잡고 실패한다. 실제로 그랬다. bake-ami.sh 가
# 지워도 그 뒤에 돌리라고 안내한 smoke-test.sh / verify-ami.sh 가 되살린다.
#
# 그래서 둘을 나눈다.
#   - credentials / config : 정적 키다. 있으면 AMI를 공유하는 순간 같이 나간다 → 실패
#   - cli/cache/session.db : CLI 텔레메트리용이다. 열어보니 테이블이
#     session(key, session_id, timestamp) 와 host_id(key, id) 뿐이고 키 문자열은
#     없었다. 보안 문제는 아니지만 **호스트 식별자**라서, 구워두면 이 AMI로 뜬
#     인스턴스가 전부 같은 id를 공유한다(machine-id 와 같은 성격). 아래에서 지운다.
#
# sudo -n 을 쓴다. 비밀번호 프롬프트로 sudo가 실패하면 test 도 실패하는데,
# 그걸 "파일 없음"으로 읽으면 확인하지 못한 것을 통과시킨다.
for p in /root/.aws /home/ubuntu/.aws; do
  if ! sudo -n true 2>/dev/null; then
    warn "$p — sudo 불가라 확인하지 못했다(직접 봐야 한다)"
  elif sudo -n sh -c "[ -f '$p/credentials' ] || [ -f '$p/config' ]"; then
    bad "$p 에 정적 자격증명이 있다 — 인스턴스 역할을 쓰므로 있어선 안 된다"
  else
    ok "$p 정적 자격증명 없음"
  fi
done
# 가디언 실패 카운터가 구워지면 안 된다. 이 AMI로 뜨는 워커는 부팅 유예
# (GUARDIAN_GRACE_SEC)가 끝나는 그 순간 카운터를 읽는다. 0이 아니라 이미
# 임계값을 넘은 값이 들어있으면, 리퍼가 잠시 없을 뿐인 정상 워커도 그
# 자리에서 곧바로 종료된다 — 더 봐주기로 한 FAIL_MIN 분이 통째로 사라진다.
# 지금은 /run/emr (tmpfs) 라 구조적으로 불가능하지만, 누군가 STATE 를 다시
# 영구 디스크로 되돌리면 조용히 돌아온다. 그걸 여기서 잡는다.
if [ -e /var/lib/emr/guardian.fail ]; then
  bad "/var/lib/emr/guardian.fail 이 남아있다 — 구워지면 워커가 조기 종료된다"
else
  ok "가디언 카운터가 영구 디스크에 없음(tmpfs 사용)"
fi
# conda 패키지 캐시는 환경을 만들고 나면 쓸모가 없는데 35GB까지 부푼다.
# 스냅샷은 쓴 블록만 세므로, 이걸 안 지우면 매달 그 35GB만큼 돈을 더 낸다.
PKGS=$(du -sm "$EMR_ROOT/miniconda3/pkgs" 2>/dev/null | cut -f1 || echo 0)
if [ "${PKGS:-0}" -gt 2048 ]; then
  warn "conda 패키지 캐시가 $(( PKGS / 1024 ))GB 남아있다 — 'conda clean -a -y' 로 지우면 스냅샷이 그만큼 작아진다"
else
  ok "conda 패키지 캐시 정리됨 (${PKGS}MB)"
fi
df -h / | awk 'NR==2 {printf "  ✔ 루트 %s 중 %s 사용 (여유 %s)\n", $2, $3, $4}'

echo
if [ "$FAIL" -eq 0 ]; then
  # **여기가 인스턴스에서 도는 마지막 코드다.** 다음 단계인 create-image 는 개발
  # PC에서 부른다. config.sh 를 읽는 스크립트는 전부 CLI 캐시를 되살리므로,
  # 지우고도 안 되살아나는 자리는 여기뿐이다. 검사가 다 통과했을 때만 지운다.
  sudo -n rm -rf /root/.aws/cli /home/ubuntu/.aws/cli 2>/dev/null \
    && echo "  ✔ CLI 세션 캐시 제거(호스트 식별자가 AMI에 굳지 않게)" \
    || echo "  ! CLI 세션 캐시를 못 지웠다 — sudo rm -rf ~/.aws/cli 를 직접 돌릴 것"
  echo
  echo "✔ 검증 통과 — 스냅샷을 찍어도 된다."
  # 명령을 통째로 찍는다. 예전엔 "<이 인스턴스>" 자리와 태그를 손으로 채우게
  # 뒀는데, GitSha 태그가 빠진 AMI 가 나왔다. 20-launch-template.sh:45 는 그
  # 태그가 없으면 하드 실패하므로, 굽고 한참 뒤 배포 단계에서야 알게 된다.
  # 어느 스크립트도 이 태그를 안 붙인다 — 붙이는 곳은 이 명령 하나뿐이다.
  # IMDSv2 전용이다(시작 템플릿이 HttpTokens=required 로 잠갔다).
  _T=$(curl -sf -X PUT "http://169.254.169.254/latest/api/token" \
         -H "X-aws-ec2-metadata-token-ttl-seconds: 60" --max-time 2 || echo "")
  IID=$(curl -sf -H "X-aws-ec2-metadata-token: $_T" --max-time 2 \
         "http://169.254.169.254/latest/meta-data/instance-id" || echo "")
  echo "  aws ec2 create-image --region $AWS_REGION \\"
  echo "    --instance-id ${IID:-<이 인스턴스>} --name emr-gpu-$EMR_IMAGE_TAG --no-reboot \\"
  echo "    --tag-specifications \\"
  echo "      'ResourceType=image,Tags=[{Key=GitSha,Value=$EMR_IMAGE_TAG},{Key=Project,Value=emr},{Key=Name,Value=emr-gpu-$EMR_IMAGE_TAG}]' \\"
  echo "      'ResourceType=snapshot,Tags=[{Key=GitSha,Value=$EMR_IMAGE_TAG},{Key=Project,Value=emr}]'"
else
  echo "✗ 검증 실패 — 이대로 구우면 부팅은 되고 요금만 나가는 인스턴스가 된다."
fi
exit "$FAIL"
