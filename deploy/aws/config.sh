#!/bin/bash
# 4단계 공통 설정. 나머지 스크립트가 전부 이 파일을 source 한다.
# 여기 한 곳만 고치면 되게 해두면, 이름이 어긋나서 "리소스는 만들어졌는데
# 서로 못 찾는" 사고를 막을 수 있다(2단계 큐 리전 사고와 같은 종류다).

export AWS_REGION=ap-northeast-2
export AWS_DEFAULT_REGION=$AWS_REGION

PROJECT=emr

# ── 이름 ────────────────────────────────────────────────────────────────
export Q_SAM3D=emr-sam3d
export Q_SCENE=emr-scene
# S3 버킷 이름은 **전 세계에서 유일**해야 한다(계정별이 아니다). 그냥 emr-jobs 로
# 두면 언제든 다른 사람이 먼저 가져가서 배포가 중간에 막힌다.
#
# 계정 ID를 접미사로 붙이는 게 흔한 방법이지만 쓰지 않았다. 브라우저가 결과
# 메쉬를 presigned URL 로 S3에서 직접 받으므로, 버킷 이름이 곧 사용자에게
# 보이는 URL 에 들어간다 — 계정 ID를 거기 노출할 이유가 없다. 대신 계정 ID의
# 해시 앞 8자리를 쓴다. 같은 계정이면 항상 같은 값이라 따로 저장할 필요가 없고,
# 역으로 계정 ID를 알아낼 수는 없다.
#
# 로컬(LocalStack)은 전역 유일성이 필요 없어서 그냥 emr-jobs 를 쓴다. 이름이
# 다른 건 의도된 것이고, 코드는 양쪽 다 환경변수로만 읽는다.
if [ -z "${S3_BUCKET:-}" ]; then
  _acct=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")
  if [ -n "$_acct" ]; then
    export S3_BUCKET="emr-jobs-$(printf '%s' "$_acct" | sha256sum | cut -c1-8)"
  else
    export S3_BUCKET="emr-jobs"   # 자격증명 없이 소스될 때(문법 검사 등)
  fi
  unset _acct
fi
export DDB_TABLE=emr-jobs

export LT_NAME=${PROJECT}-gpu-worker            # 시작 템플릿
export ASG_SPOT=${PROJECT}-gpu-spot             # 평소 쓰는 스팟 그룹
export ASG_OD=${PROJECT}-gpu-ondemand           # 스팟이 안 뜰 때만 쓰는 폴백 그룹
export ROLE_NAME=${PROJECT}-gpu-worker-role
export PROFILE_NAME=${PROJECT}-gpu-worker-profile

# ── 용량 ────────────────────────────────────────────────────────────────
# ①(세 컨테이너 한 대) 구성이라 인스턴스 1대 = sam3d 1건 + scene 1건 동시 처리다.
#
# 2026-10-01 현재 쿼터에 맞춰 2대로 묶어 뒀다(스팟 8 vCPU ÷ 4 = 2대).
# 24 vCPU 신청은 아직 열려 있고(CASE_OPENED), 승인되면 3으로 되돌리고
# 아래 INSTANCE_TYPES 에 2xlarge 를 다시 넣으면 된다.
export MAX_SPOT=2       # 스팟 그룹 최대
export MAX_OD=1         # 폴백은 1대면 충분하다. 여기가 커지면 비용이 튄다.

# ── 스케일아웃 판단 ─────────────────────────────────────────────────────
# 백로그 = 두 큐의 (대기 + 처리중) 메시지 합.
# BACKLOG_STEP1 이하면 +1대, 넘으면 +2대.
export BACKLOG_STEP1=10
# 부팅 + 모델 로드까지 걸리는 시간. 이 시간 동안은 방금 띄운 인스턴스를
# "아직 일 못 하는 중"으로 쳐서 추가 스케일아웃을 억제한다. 안 그러면
# 첫 인스턴스가 준비되기 전에 알람이 또 울려서 필요 없는 대수를 띄운다.
export WARMUP_SEC=300

# 스팟이 이만큼(분) 한 대도 못 뜨면 온디맨드 폴백을 깨운다.
# ASG에는 스팟→온디맨드 자동 폴백이 없어서 우리가 직접 만드는 장치다.
export OD_FALLBACK_MIN=5

# ── 인스턴스 후보 ───────────────────────────────────────────────────────
# 전부 24GB급 단일 GPU다. 후보를 넓힐수록 스팟 풀이 늘어 "용량 없음"이 줄어든다.
# g6e(L40S 48GB)는 비싸지만, 못 뜨는 것보단 나아서 맨 뒤에 둔다.
#
# 2xlarge(g6.2xlarge, g5.2xlarge)를 뺐다. GPU가 같아서 성능상 문제는 없지만
# **vCPU가 8이라 한 대로 쿼터를 다 쓴다.** 쿼터가 8 vCPU인 지금 후보에 남겨두면
# ASG가 그걸 고르는 순간 두 번째 인스턴스가 VcpuLimitExceeded 로 못 뜬다.
# 그 실패는 ASG 활동 기록에만 남고 애플리케이션 쪽에는 "그냥 느림"으로 보인다.
# 쿼터가 24로 올라가면 다시 넣는다(위 MAX_SPOT 주석 참고).
export INSTANCE_TYPES="g6.xlarge g5.xlarge g6e.xlarge"

# ── AMI / 이미지 ────────────────────────────────────────────────────────
# conda 환경 · 모델 소스 · 가중치 · 저장소가 인스턴스에서 모두 이 아래 모인다.
# 컨테이너도 **같은 절대경로**로 마운트한다(conda prefix와 editable 설치가
# 경로를 박아두기 때문 — deploy/gpu/Dockerfile 머리말 참고).
#
# 이전에는 개발 PC의 홈 경로(/home/tmvlem5671/...)가 이미지와 compose에 박혀
# 있었다. EC2 기본 사용자는 ubuntu라 그 경로가 없으니 컨테이너가 아예 못 떴고,
# 그러면 리퍼도 없어서 인스턴스가 일 없이 영원히 과금되는 경로로 직행했다.
# 로컬에서는 EMR_ROOT를 비워두면 compose가 ${HOME}을 쓴다(= 전과 동일).
export EMR_ROOT=${EMR_ROOT:-/opt/emr}

# 인스턴스 안에서 저장소가 어디에 있는지. userdata가 여기로 cd 한다.
# compose의 sam3d/backend 마운트와 반드시 같아야 한다($EMR_ROOT/Empty_My_Room).
export EMR_REPO_DIR=${EMR_REPO_DIR:-$EMR_ROOT/Empty_My_Room}

# AMI에 구워 넣은 도커 이미지의 태그. bake-images.sh가 git SHA로 붙인다.
# userdata는 부팅 때 이 태그가 실제로 있는지 확인하고, 없으면 빌드를 시도하지
# 않고 크게 실패한다 — 부팅 중 빌드는 콜드스타트를 수십 분으로 늘린다.
export EMR_IMAGE_TAG=${EMR_IMAGE_TAG:-$(git -C "$(dirname "${BASH_SOURCE[0]}")/../.." rev-parse --short HEAD 2>/dev/null || echo dev)}

# 부팅 때 미리 읽어둘 디렉터리(스냅샷 지연 로딩 해소용). 공백으로 구분.
# 장치명(/dev/nvme1n1)을 찍지 않는다 — 장치 번호는 볼륨 구성에 따라 바뀌고,
# 틀려도 조용히 넘어가서 워밍이 아예 안 된 걸 모른다. 파일 단위로 읽으면
# 장치와 무관하고, 필요한 것만 읽어서 볼륨 전체보다 빠르다.
#
# 순서가 곧 우선순위다(상한에 걸리면 뒤쪽은 첫 요청 때 읽힌다). 실제로 콜드스타트를
# 지배하는 건 모델 가중치(HF 캐시 28G)와 conda 환경의 큰 .so(torch/cuda, 32G)다.
# 소스 디렉터리(3.9G)는 대부분 작은 파일이라 >8M 필터에 거의 안 걸린다 — 뒤에 둔다.
# (예전에는 $EMR_REPO_DIR/sam-3d-objects 처럼 **존재하지 않는** 경로를 가리켰고,
#  warm()은 없는 디렉터리를 조용히 건너뛰므로 워밍이 0바이트인 걸 알 수 없었다.)
export EMR_WARM_DIRS=${EMR_WARM_DIRS:-"$EMR_ROOT/.cache/huggingface $EMR_ROOT/miniconda3/envs $EMR_ROOT/sam-3d-objects $EMR_ROOT/uLayout $EMR_ROOT/omni3d"}
# 워밍에 쓸 최대 시간(초). 올리면 GUARDIAN_GRACE_SEC 도 같이 올라간다(아래).
export EMR_WARM_TIMEOUT=${EMR_WARM_TIMEOUT:-420}

# 동시에 읽을 파일 수. 지연 로딩은 대역폭이 아니라 왕복 지연에 묶여 있어서,
# 한 줄로 읽으면 볼륨 처리량의 몇 %밖에 못 쓴다 — 첫 실부팅에서 250 MB/s
# 짜리 볼륨에서 10.5 MB/s 가 나왔다. vCPU 4개짜리에 32는 과해 보이지만 CPU를
# 쓰는 일이 아니라 IO를 기다리는 일이라 상관없다. 메모리도 32 x 4MB = 128MB다.
export EMR_WARM_JOBS=${EMR_WARM_JOBS:-32}

# 워밍에서 뺄 경로 조각(공백 구분, grep -E 로 묶어서 제외한다).
# AMI의 HF 캐시 28GB에는 **배포하는 세 서비스가 안 쓰는** 모델이 섞여 있다.
# 빌더에서 홈 디렉터리째 tar로 떠왔기 때문이다. 근거:
#   control_v11p_sd15_canny / stable-diffusion-inpainting (5.4G)
#     → sam3d/backend/services/sd_service.py 만 쓰는데, 그 모듈은 워커·API
#       어느 쪽에서도 import 되지 않는다(사용자가 SD/인페인팅을 보류했다).
#   zero123plus-v1.2 (5.2G) / TripoSR (1.6G)
#     → 저장소 코드와 벤더 코드(sam-3d-objects, uLayout, omni3d) 어디에서도
#       참조가 없다. SAM3D로 정착하기 전에 실험하던 대안 모델들이다.
# 합쳐 12.2GB. 이걸 빼면 워밍 대상이 60GB에서 43GB가 된다.
#
# 틀렸을 때의 대가는 작다. 필요한 걸 실수로 빼도 기동은 멀쩡하고 그 모델만
# 첫 요청 때 느리게 읽힌다. 반대로 안 쓰는 걸 데우면 **매 콜드스타트마다**
# 그만큼 시간을 버린다. 그래서 확신이 서는 것만 뺐다 — moge-2-vitl(1.3G)과
# depth_anything_vitl14(1.3G)도 참조를 못 찾았지만 금액이 작아서 남겨뒀다.
export EMR_WARM_SKIP=${EMR_WARM_SKIP:-"models--lllyasviel--control_v11p_sd15_canny models--runwayml--stable-diffusion-inpainting models--sudo-ai--zero123plus models--stabilityai--TripoSR"}

# ── 루트 볼륨 ───────────────────────────────────────────────────────────
# AMI에 들어가는 실제 내용물은 **약 123GB다**. 우리가 넣는 것만 세면 85GB지만
# (conda 39 + HF 캐시 28 + torch 캐시 3.4 + 소스/체크포인트 3.3 + 저장소 0.3 +
# 이미지 3 + OS/드라이버 8), 빌더에서 재어보니 123G였다 — Deep Learning Base AMI
# 자체가 생각보다 크다(CUDA 툴킷 여러 버전, 드라이버, 도커 이미지).
#
# conda 39GB는 `du --count-links` 로 센 값이다. 그냥 `du -sh` 로 세면 32GB가
# 나오는데, uLayout과 omni3d가 Python 3.10을 공유해서 하드링크 7GB가 한 번만
# 세어진 결과다. conda-pack 산출물에는 하드링크가 없으니 EC2가 쓰는 건 39쪽이다.
#
# 굽는 도중 피크도 추가 복사본 없이 그 안에서 끝난다. bootstrap-ami.sh 가 S3에서 **파이프로 바로** 풀어서
# 중간 tarball도, conda 패키지 캐시도 만들지 않는다. (EC2에서 conda create 로
# 재설치하는 계획이었다면 ~/miniconda3/pkgs 가 35GB까지 부풀어 피크가 115GB였다.)
#
# 그래도 150GB로 잡는다. 스냅샷 요금은 **쓴 블록만** 세므로(약 123GB x $0.05 =
# 월 $6.4) 안 쓴 27GB는 돈을 안 낸다. 인스턴스가 떠 있는 동안의 볼륨 요금만 150GB
# 기준이고, 스케일투제로라 그 시간이 짧다.
#
# 빌더 인스턴스도 **같은 150GB로** 띄워야 한다. AMI 스냅샷 크기가 빌더의 볼륨
# 크기로 굳고, 그게 이 값의 하한이 된다 — 더 크게 띄우면 20-launch-template.sh 가
# 거부한다.
export EMR_VOLUME_GB=${EMR_VOLUME_GB:-150}

# gp3 기본값은 125 MB/s 다. 스냅샷에서 복원한 볼륨은 블록을 처음 읽을 때
# S3에서 끌어오므로(지연 로딩), 이 숫자가 부팅 워밍 속도의 상한이 된다.
# 125 MB/s면 80GB를 다 데우는 데 10분 이상이라 EMR_WARM_TIMEOUT(600초)에 걸린다.
# 올리면 빨라지지만 125 초과분은 MB/s당 월 $0.04다(250이면 월 $5, 떠 있는
# 시간만큼 비례 과금). 첫 실부팅 로그의 [warm] 줄을 보고 조정한다.
export EMR_VOLUME_THROUGHPUT=${EMR_VOLUME_THROUGHPUT:-250}

# ── 가디언(요금 폭주 차단) ──────────────────────────────────────────────
# 리퍼는 컴포즈 스택 안의 컨테이너다. 그래서 컴포즈가 안 뜨면 리퍼도 없고,
# ASG 헬스체크는 EC2(켜져 있는지)만 보므로 인스턴스가 일 없이 계속 과금된다.
# 가디언은 호스트에서 systemd 타이머로 돌며 "리퍼가 살아있나"만 본다.
#
# 유예는 **부팅이 끝날 수 있는 시간보다 길어야 한다.** 짧으면 멀쩡히 부팅
# 중인 워커를 죽인다. 실제로 그랬다: 2026-10-01, 유예 900 + FAIL_MIN 5분이
# 끝나는 20.2분에 아직 워밍 중이던 워커가 회수됐다. 두 숫자(워밍 상한과
# 가디언 유예)가 같은 파일에 따로 적혀 있어서 조용히 어긋난 것이다.
# 그래서 손으로 맞추지 않고 **유도한다**.
#   부트+cloud-init(~120) + 워밍 상한 + 컴포즈·헬스체크(~600) + 여유(300)
# EMR_WARM_TIMEOUT 을 올리면 유예도 같이 올라간다.
#
# 주의: 이 값은 bake-ami.sh 가 emr-guardian.service 의 Environment= 에
# **구워 넣는다.** 이미 구운 AMI에는 옛 값이 들어 있으므로, userdata.sh 가
# 부팅할 때 systemd 드롭인으로 덮어쓴다(EMR_IMAGE_TAG 와 같은 함정이다 —
# 배포 시점 설정이 AMI에 굳은 값에 지면 안 된다).
export GUARDIAN_GRACE_SEC=${GUARDIAN_GRACE_SEC:-$(( 120 + EMR_WARM_TIMEOUT + 600 + 300 ))}
export GUARDIAN_FAIL_MIN=${GUARDIAN_FAIL_MIN:-5}       # 리퍼 부재가 이만큼(분) 이어지면 회수
