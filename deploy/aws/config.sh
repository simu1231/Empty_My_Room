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

# ── 알림 ────────────────────────────────────────────────────────────────
# DLQ 에 메시지가 쌓이면 메일을 받는다. DLQ 는 "재시도를 다 쓰고도 안 된 작업"이
# 모이는 곳이라, 조용히 쌓이면 아무도 모르는 사이에 사용자 작업이 사라진다.
#
# 주의: non-retryable 로 분류돼 즉시 삭제된 작업은 **DLQ 에 오지 않는다.**
# 그건 DynamoDB 의 failure_kind 로만 보인다(worker.py 의 except 분기 참고).
# 이 알람이 커버하는 건 "재시도를 소진한" 쪽이다.
export EMR_ALERT_EMAIL=${EMR_ALERT_EMAIL:-wyr24353354@gmail.com}
export EMR_ALERT_TOPIC=${EMR_ALERT_TOPIC:-${PROJECT:-emr}-alerts}
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
# 폴백은 남기되 **조용히** 떨어지지 않게 한다. 예전에는 aws 가 PATH 에 없으면
# 말없이 emr-jobs(이 계정에 없는 버킷)로 떨어졌다. 그러면 업로드가 한참 뒤에
# NoSuchBucket 으로 죽는데, 원인은 버킷이 아니라 PATH 다 — 20-launch-template.sh
# 에서 똑같은 구조로 "AMI 에 GitSha 태그가 없습니다"를 찍고 한참 헤맸다.
# 원인과 메시지를 일치시키고, 추측값이라는 사실을 EMR_BUCKET_GUESSED 로 남긴다.
export EMR_BUCKET_GUESSED=0
if [ -z "${S3_BUCKET:-}" ]; then
  if ! command -v aws >/dev/null 2>&1; then
    export S3_BUCKET="emr-jobs"; export EMR_BUCKET_GUESSED=1
    echo "⚠ aws CLI 가 PATH 에 없어 S3_BUCKET 을 추측했다: $S3_BUCKET (실제와 다를 수 있다)" >&2
    echo "  이 PC 에서는 ~/.local/bin 에 있다: export PATH=\"\$HOME/.local/bin:\$PATH\"" >&2
  else
    _acct=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")
    if [ -n "$_acct" ]; then
      export S3_BUCKET="emr-jobs-$(printf '%s' "$_acct" | sha256sum | cut -c1-8)"
    else
      export S3_BUCKET="emr-jobs"; export EMR_BUCKET_GUESSED=1
      echo "⚠ AWS 자격증명을 읽지 못해 S3_BUCKET 을 추측했다: $S3_BUCKET (실제와 다를 수 있다)" >&2
      echo "  aws configure 로 설정하거나, 로컬 테스트면 그대로 둬도 된다(LocalStack 은 이 이름을 쓴다)" >&2
    fi
    unset _acct
  fi
fi
export DDB_TABLE=emr-jobs

# ── 로그 ────────────────────────────────────────────────────────────────
# 컨테이너 로그를 CloudWatch 로 보낼 그룹. 15-resources.sh 가 보존기간 7일로
# 미리 만들고, userdata.sh 가 awslogs 드라이버에 이 이름을 넘긴다.
#
# 그룹은 **하나**로 두고 서비스를 스트림으로 가른다(tag: "{{.Name}}/{{.ID}}").
# 서비스마다 그룹을 쪼개면 사이드카(ulayout/omni3d)와 워커의 시간 순서를
# 맞춰 볼 수 없는데, 6단계에서 보고 싶은 게 바로 그 순서다 —
# "sam3d 첫 건 1230초" 동안 어느 컨테이너가 디스크를 쥐고 있었는지.
export LOG_GROUP=${LOG_GROUP:-/emr/worker}

export LT_NAME=${PROJECT}-gpu-worker            # 시작 템플릿
export ASG_SPOT=${PROJECT}-gpu-spot             # 평소 쓰는 스팟 그룹
export ASG_OD=${PROJECT}-gpu-ondemand           # 스팟이 안 뜰 때만 쓰는 폴백 그룹
export ROLE_NAME=${PROJECT}-gpu-worker-role
export PROFILE_NAME=${PROJECT}-gpu-worker-profile

# ── 용량 ────────────────────────────────────────────────────────────────
# ①(세 컨테이너 한 대) 구성이라 인스턴스 1대 = sam3d 1건 + scene 1건 동시 처리다.
#
# 2026-10-07 현재 쿼터에 맞춰 1대다(스팟 8 vCPU ÷ 8 = 1대).
# 2026-10-01 에는 4 vCPU 짜리 xlarge 를 써서 2대였는데, 아래 INSTANCE_TYPES
# 주석대로 xlarge 가 SAM3D 를 못 돌린다는 게 실측으로 드러나 8 vCPU 짜리
# 2xlarge 로 갈아탔다. 대수는 줄었지만 "안 도는 2대"보다 "도는 1대"가 낫다.
#
# 24 vCPU 신청은 아직 열려 있다(CASE_OPENED). 승인되면 여기를 3으로 올린다.
# **승인돼도 xlarge 는 돌아오지 않는다** — 쿼터 문제가 아니라 RAM 문제였다.
export MAX_SPOT=1       # 스팟 그룹 최대
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
# 전부 24GB급 이상 단일 GPU다. 후보를 넓힐수록 스팟 풀이 늘어 "용량 없음"이 줄어든다.
# g6e(L40S 48GB)는 비싸지만, 못 뜨는 것보단 나아서 맨 뒤에 둔다.
#
# ── xlarge 를 전부 뺐다 (2026-10-07 실측) ───────────────────────────────
# 한동안 여기엔 xlarge 만 있었다. "GPU가 같으니 성능도 같다"고 봤는데 틀렸다.
# **먼저 터지는 건 VRAM 이 아니라 Host RAM 이다.**
#
#   SAM3D 콜드 실행 RSS peak   15,238 MB
#   g6.xlarge 총 RAM           15,368 MB   ← 차이 130 MB
#
# 페이지 캐시에 쓸 자리가 0이라 커널이 **곧 다시 읽을 페이지를 쫓아낸다.**
# 그 결과가 스래싱이고, 증상은 이렇게 나타난다:
#   - EBSReadOps 가 gp3 상한(3,000)에 고정 — 실측 3,210 IOPS
#   - CPUUtilization / NetworkOut 은 평평 (일을 안 하는 게 아니라 못 한다)
#   - **SSH 가 banner exchange 에서 타임아웃** — sshd 가 자기 자식 바이너리를
#     페이지인 못 해서다. 보안그룹 문제로 착각하기 딱 좋다(SG 는 TCP 에서 막힌다).
# 그래서 ASG 활동 기록에도, 알람에도 안 걸린다. 그냥 "큐가 안 빠진다"로 보인다.
#
# 같은 작업이 g6.2xlarge(30.9GB)에서는 976초에 완주하고 EBS 는 52 IOPS 다.
# VRAM peak 는 20,359 / 23,034 MiB (88%) 로 **타입과 무관**하다 — 24GB급이면
# 다 통과한다. 즉 고르는 기준은 GPU 가 아니라 RAM 이고, 하한은 30GB 다.
#
# g6e.xlarge(RAM 32GB)는 RAM 만 보면 통과지만 뺐다. vCPU 가 4라 2xlarge 와
# 섞어도 05-preflight.sh 의 최악값은 어차피 8 로 고정되어 실익이 없고,
# 셋 중 제일 비싸다.
#
# ── g6e.2xlarge 를 뺐다 (2026-10-07 실측) ───────────────────────────────
# "후보가 많을수록 안전하다"가 여기서는 거꾸로였다. ap-northeast-2 의 제공
# 현황이 타입마다 다르다:
#
#   AZ   g6.2xlarge  g5.2xlarge  g6e.2xlarge
#   2a       O           O            O
#   2b       X           X            O      ← g6e 만 있는 유일한 AZ
#   2c       O           O            X
#   2d       O           O            X
#
# ASG 는 AZ 와 타입을 독립적으로 고르므로 2b+g6, 2d+g6e 같은 **존재하지 않는
# 조합**을 집어 든다. 그때 돌아오는 건 "용량 없음"이 아니라
# InvalidFleetConfiguration 이고, 활동 기록에 Failed 로 남은 뒤 약 1분을
# 버리고 다시 고른다. 사용자에게는 그냥 "GPU 가 안 뜬다"로 보인다.
#
# g6e 를 빼면 남는 두 타입의 제공 AZ 가 {2a,2c,2d} 로 완전히 겹쳐서, 어떤
# 조합을 골라도 유효하다. 대신 2b 를 서브넷에서 빼야 한다 — 아래 30-asg.sh
# 가 INSTANCE_TYPES 로부터 그 교집합을 직접 계산하므로 손으로 맞출 일은 없다.
export INSTANCE_TYPES="g6.2xlarge g5.2xlarge"

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
# ── 부팅 경로 워밍은 없앴다(측정 기록) ──────────────────────────────────
# 예전에는 EMR_WARM_TIMEOUT 으로 부팅 중 워밍 시간을 정했다. 그 변수는 지웠다.
# 지운 이유 — 상한을 420→210→105→0으로 깎아가며 네 번 재보니, 워밍을
# 빼는 쪽이 매번 이겼다. 콜드스타트 674 → 522 → 447 → 428 → 345초.
#
# 실측(동시 64, g6.xlarge, gp3 250 MB/s). 괄호 안은 부팅 전체 읽기량:
#   상한 420초 … find 53 + 읽기 368초, 28.3GB → 컴포즈 166초 → 총 674초 (30.5GB)
#   상한 210초 … find 53 + 읽기 158초, 13.2GB → 컴포즈 226초 → 총 522초 (15.3GB)
#   상한 105초 … find 53 + 읽기  53초,  4.3GB → 컴포즈 256초 → 총 447초 ( 7.0GB)
#   상한   0초 … find 53 + 읽기   0초,     0  → 컴포즈 286초 → 총 428초 (2.89GB)
#   함수 생략  … find 없음               0  → 컴포즈 266초 → 총 345초
#
# 이 표가 말하는 건 세 가지다.
#
# 1) 상한에는 **find 53초가 숨어 있었다.** 읽기를 0으로 두고도 워밍 단계가
#    53초를 먹었다 — 목록을 만드는 find 가 파일 수십만 개의 inode 를 지연
#    로딩으로 긁는다. 그래서 상한만 0으로 두는 건 끄는 게 아니다. 함수를
#    아예 부르지 않아야 그 53초가 사라진다(428 → 345초).
#    같은 이유로 "짧은 상한일수록 처리량이 떨어진다"는 예전 해석은 틀렸다.
#    53초를 빼고 계산하면 77/83/84 MB/s 로 사실상 일정하다.
#
# 2) 부팅에 실제로 필요한 건 **2.89GB뿐이다.** 워밍을 끈 부팅이 전체에서
#    그만큼만 읽었다. 워밍 대상은 47GB였으니 94%는 부팅과 무관했다.
#    워밍의 적중률이 420초짜리에서도 5~7%에 머문 이유가 이것이다.
#
# 3) 그런데도 **워밍이라는 수단 자체는 세다.** 컴포즈 중 읽기는 10 MB/s
#    (지연 로딩을 순차로 맞으니까)인데 워밍 중에는 80 MB/s 다(64갈래로 겹쳐
#    왕복 지연을 숨긴다). 8배 차이다. 문제는 수단이 아니라 **시점과 대상**
#    이었다: 부팅을 막으면서, 부팅이 안 쓰는 데이터를 데우고 있었다.
#
# 그래서 워밍은 없애지 않고 기동 **뒤로** 옮겼다 — EMR_WARM_BG_DIRS 참고.
# 상한이라는 개념도 같이 사라졌다. 부팅을 막지 않으니 깎을 이유가 없다.

# 동시에 읽을 파일 수. 지연 로딩은 대역폭이 아니라 왕복 지연에 묶여 있어서,
# 한 줄로 읽으면 볼륨 처리량의 몇 %밖에 못 쓴다. vCPU 4개짜리에 64는 과해
# 보이지만 CPU를 쓰는 일이 아니라 IO를 기다리는 일이라 상관없다. 메모리도
# 64 x 4MB = 256MB다.
#
# 실측(CloudWatch AWS/EBS VolumeReadBytes, 250 MB/s gp3):
#   순차  1개 … 10.5 MB/s   콜드스타트 21.8분
#   동시 32개 …   70 MB/s   콜드스타트 10.7분
# 32에서도 프로비전의 28%밖에 못 쓰고 있어서 64로 올렸다. 더 올려도
# 안 오르는 지점이 오면 거기가 지연 로딩 자체의 동시성 한계다 —
# 그때는 이 값이 아니라 **워밍 대상**을 줄이는 쪽으로 가야 한다.
export EMR_WARM_JOBS=${EMR_WARM_JOBS:-64}

# 워밍에서 뺄 경로 조각(공백 구분, grep -E 로 묶어서 제외한다).
# AMI의 HF 캐시에는 **배포하는 세 서비스가 안 쓰는** 모델이 섞여 있다.
# 빌더에서 홈 디렉터리째 tar로 떠왔기 때문이다.
#
#   control_v11p_sd15_canny (1.4G) / stable-diffusion-inpainting (4.0G)
#   zero123plus-v1.2 (5.2G) / TripoSR (1.6G)
#     → 넷 다 **deploy/ 파이프라인은 안 쓰고 web/backend 가 쓴다.**
#       web/backend/main.py 가 기동 때 Zero123Service·TripoSRService 를 올리고
#       라우터(/api/zero123, /api/triposr)와 프런트(RelocateStep.jsx)가 부른다.
#       sd/canny 는 sd_service.py·lama 가 쓴다. 그러니 **디스크에 남긴다** —
#       데우지만 않는다.
#
# 한때 이 주석은 넷 다 "참조가 없다"고 적어 뒀고, 그 말을 믿고 12.2GB 를
# 지울 뻔했다. 둘은 사용자가 잡아냈고 나머지 둘은 지우기 직전 grep 에서
# 걸렸다. **grep 범위를 deploy/ 로 좁혀 놓고 "저장소 전체에 없다"고 쓴 것**이
# 원인이다. 지우는 쪽 판단은 범위를 먼저 말하고 적는다.
#
# 이 목록은 이제 안전망에 가깝다. EMR_WARM_BG_DIRS 가 디렉터리 훑기에서
# 파일 네 개를 콕 집는 방식으로 바뀌어서, 여기 뭘 적든 실제로 걸러낼 후보가
# 없다. 목록을 다시 넓힐 때를 대비해 남겨 둔다.
export EMR_WARM_SKIP=${EMR_WARM_SKIP:-"models--lllyasviel--control_v11p_sd15_canny models--runwayml--stable-diffusion-inpainting models--sudo-ai--zero123plus models--stabilityai--TripoSR"}

# ── 기동 뒤 백그라운드 워밍 ──────────────────────────────────────────────
# 부팅 경로에서 워밍을 빼면 345초에 기동하지만, 대신 **첫 추론 요청**이
# 지연 로딩을 통째로 떠안는다. 스모크 테스트에서 sam3d 첫 요청이 187초였고
# 두 번째는 56초였다 — 그 131초 차이가 HF 캐시(28GB)를 S3에서 끌어오는 값이다.
#
# 워밍이 필요 없던 게 아니라 **부팅을 막아선 안 됐던** 것이다. 그래서 같은
# 일을 "기동 완료"를 선언한 뒤 별도 systemd 유닛으로 돌린다. 워커가 준비됐다고
# 알리는 시점은 345초로 당겨지고, 첫 요청 지연도 잃지 않는다.
#
# cloud-init 안에서 `&` 로 띄우면 안 된다. 상속한 fd 를 붙들고 있으면
# cloud-init 이 끝난 것으로 보이지 않고, 가디언의 "cloud-init running 이면
# 판단 보류" 가 풀리지 않는다(= 고장난 워커를 회수하지 못한다). 진행 표시
# 서브셸의 sleep 이 init 에 입양됐던 것과 같은 함정이라 systemd 로 떼어낸다.
#
# 대상. 예전엔 `$EMR_WARM_DIRS` 를 그대로 썼다 — "뒤에서 도니까 시간 예산을
# 아낄 이유가 없고, 부팅이 안 쓰는 94%도 결국 추론에서는 쓰인다"는 논리였다.
# 그 논리가 틀렸다. 빌더에서 재 보고 알았다.
#
#   ① 이 기계의 페이지 캐시는 **7GB 안팎**이다(RAM 15GiB - 컨테이너 셋).
#      목록은 46GB였다. 46GB를 읽으면 뒤에 읽은 게 앞에 읽은 것을 밀어낸다.
#      다 읽으면 다 식는다 — 워밍이 자기가 한 일을 스스로 지우고 있었다.
#
#   ② conda env 워밍은 **적중률 4%** 다. 콜드 상태에서 import torch +
#      파이프라인 생성을 돌리고 fincore 로 재니, 목록에 든 13.19GB 중 실제로
#      올라온 건 0.57GB뿐이었다. .so 는 mmap 이라 **건드린 페이지만** 상주한다
#      (libtorch_cuda.so 864MB → 43MB, open3d pybind 754MB → 34MB). dd 로
#      통째로 읽어 봐야 그 중 4%만 쓰인다. 13GB 예산을 0.5GB 값에 쓰는 셈이다.
#
#   ③ 8GB 넘게 데우는 건 어차피 못 쓴다. ss_generator.ckpt(6.23GB)를 끝까지
#      읽었는데 끝난 뒤 상주는 2,214MB 뿐이었다 — free 가 7,632MB 나 남아
#      있는데도 그렇다. torch.load 가 쓰는 자기 버퍼가 갓 데운 캐시를 밀어낸다.
#
# 그래서 워밍 자체를 버리는 게 아니라 **과녁**을 바꾼다. 기법은 여전히 옳다:
# torch.load 는 zip 안의 텐서 수천 개를 걸어다니느라 23 MB/s 로 읽는데
# (6.23GB / 267초), 64갈래 dd 는 80 MB/s 다. 3.5배 차이다.
#
# 넣는 것 — worker-sam3d 의 **첫 요청 경로**뿐이다. 근거는 체크포인트의
# pipeline.yaml 과 콜드 로드 로그에서 읽는 순서 그대로다.
# 빼는 것 — conda env 전부(위 ②), uLayout·omni3d(②번 수정으로 기동 때 스스로
# 데운다), depth_pro(1.8GB, 파이프라인은 MoGe 를 쓴다), sam-3d-objects 소스 트리.
#
# slat_generator.ckpt 는 "예산 초과"를 이유로 여기 빠져 있었다. 그 판단의 값을
# 2026-10-08 실측으로 치렀다(job 672aaa09, /emr/worker):
#   ss_generator   6.23GB  데움   →   7초   (약 900 MB/s, 페이지 캐시)
#   slat_generator 4.57GB  안 데움 → 610초  (약 7.7 MB/s, 지연 로딩 1갈래)
# 같은 디렉터리의 같은 종류 파일이고 차이는 워밍뿐이다. 파이프라인 로드가
# 905초가 됐고 작업 전체가 1013초로 끝나, 브라우저 폴링 상한 900초를 넘겼다.
# 예산을 아끼는 쪽이 아니라 예산을 올리는 쪽이 옳았다 — 아래 MAX_BYTES 참고.
#
# **순서가 곧 우선순위이자 데우는 순서**다. 작은 것부터 적는다 — 추론이
# 제일 먼저 읽는 ss_generator 를 제일 **나중에** 데워야 살아남을 확률이 높다.
# (ss_generator 앞의 누적은 7.80GiB 다. 합계 14.03GiB 를 호스트 RAM 32GiB 인
#  g6.2xlarge 에 올리므로 서로 밀어낼 여유는 아직 있다 — 목록을 더 늘리면
#  이 여유부터 사라진다. 로드 순서가 ss → slat 이라 slat 을 ss 보다 **먼저**
#  적는다. 나중에 데운 쪽이 캐시에 더 오래 남는다.)
# 디코더는 **넷 다** 적는다. 처음엔 메쉬 작업이니 mesh 디코더만 쓰겠거니 하고
# gs/gs_4 를 뺐는데, inference_pipeline.py:141-154 가 파이프라인을 만들 때
# 넷을 조건 없이 올린다 — 쓰든 안 쓰든 첫 요청에서 읽힌다. 합쳐 327MB 다.
#
# backend 서비스를 같은 인스턴스에 올리면서 SAM2/LaMa 가 들어왔다(①). 둘은
# **제일 앞**이다 — 위의 "먼저 쓸 것을 나중에 데운다"와 반대로 보이지만 같은
# 규칙이다. 그 규칙은 "쓰는 시점에 캐시에 남아 있게 하라"이고, 이 둘은 컴포즈
# **기동 중에** 백엔드가 바로 읽는다(main.py 가 시작할 때 올린다). 워밍이
# 기동과 겹쳐 도니까(userdata 의 warm-bg), 데우자마자 소비된다 — 살아남을
# 걱정을 할 구간이 없다. 반대로 뒤에 두면 기동이 먼저 끝나버려 아무 쓸모가 없다.
_SAM3D_CKPT=$EMR_ROOT/sam-3d-objects/checkpoints/hf/checkpoints
export EMR_WARM_BG_DIRS=${EMR_WARM_BG_DIRS:-"\
$EMR_ROOT/sam2_repo/checkpoints/sam2.1_hiera_large.pt \
$EMR_ROOT/lama_model/big-lama/models/best.ckpt \
$_SAM3D_CKPT/ss_decoder.ckpt \
$_SAM3D_CKPT/slat_decoder_gs.ckpt \
$_SAM3D_CKPT/slat_decoder_gs_4.ckpt \
$_SAM3D_CKPT/slat_decoder_mesh.ckpt \
$EMR_ROOT/.cache/huggingface/hub/models--Ruicheng--moge-2-vitl \
$_SAM3D_CKPT/slat_generator.ckpt \
$_SAM3D_CKPT/ss_generator.ckpt"}
#
# 총량 상한. 아홉을 합치면 14.03GiB 다(실측, du -L -sb 합 15,062,917,448B):
#   sam2 0.836 + lama 0.382 + ss_decoder 0.137 + slat_decoder_gs 0.160 +
#   slat_decoder_gs_4 0.159 + slat_decoder_mesh 0.339 + moge 1.215 +
#   slat_generator 4.570 + ss_generator 6.231
# 상한을 넘기는 파일은 건너뛰므로(break 가 아니라 continue), 딱 맞춰 조여 놓으면
# 제일 값나가는 ss_generator(6.23GB)가 통째로 빠지고 그 자리를 자잘한 게 메우는
# 최악이 난다. 10G 로는 바로 그 사고가 난다 — slat_generator 를 목록에 넣으면
# 그 앞까지 누적이 7.80GiB 라, 10GiB 예산으로는 ss_generator 가 통째로 빠지고
# 7초였던 로드가 610초짜리가 된다. 고치려던 것보다 더 나빠진다.
# 15G(16,106,127,360B)면 여유가 0.97GiB(6.5%)다. 목록을 늘릴 때 이 숫자부터
# 다시 보라 — numfmt --from=iec 라 G 는 GiB 다(bake-ami.sh 2.5).
export EMR_WARM_BG_MAX_BYTES=${EMR_WARM_BG_MAX_BYTES:-15G}
#
# 청크 크기(MiB). 목록이 파일 4개로 줄면서 새 문제가 생겼다 — xargs -P 64 는
# **파일 단위**로 갈라지므로 4개짜리 목록에서는 4갈래밖에 안 돈다. 그런데
# 지연 로딩 볼륨에서 1갈래 순차는 10.5 MB/s 다(32갈래 70 MB/s). 8GB 를
# 4갈래로 읽으면 200초, 1갈래면 800초다. 그래서 bake-ami.sh 가 큰 파일을
# 128MiB 조각으로 잘라 목록에 "경로 skip count"(4MiB 블록 단위)로 적고,
# 워밍 쪽은 조각을 병렬로 읽는다. 8GB → 64조각이라 -P 64 가 다시 꽉 찬다.
export EMR_WARM_BG_CHUNK_MB=${EMR_WARM_BG_CHUNK_MB:-128}
#
# 워커 9호 실측(이 구조의 첫 측정):
#   부팅 349.66초 — 워밍 없는 345.09초와 사실상 같다(인스턴스 변동 범위).
#   cloud-init 이 349.66초에 **정상 종료**했다. 이게 제일 중요한 확인점이다.
#   `&` 로 띄웠다면 상속한 fd 를 붙들어 cloud-init 이 끝나지 않고, 그러면
#   가디언의 "running 이면 판단 보류"가 안 풀려서 고장난 워커를 회수하지
#   못한다 — 요금이 새는 쪽 고장이다. systemd-run 분리가 그걸 막았다.
#
# 다만 워밍 자체는 **한 바이트도 읽지 못했다.** 목록을 만드는 find 가 끝나기
# 전에 리퍼가 유휴 120초로 회수했다(부팅 경로에서 53초였던 find 가 io
# 우선순위 idle 에서 두 배 이상 느려졌다). 그래서 목록은 AMI 를 구울 때
# 미리 만들어 두게 바꿨다 — bake-ami.sh 2.5 단계.
#
# 작업이 없을 때 워밍이 끊기는 것 자체는 의도대로다. 일 없는 g6.xlarge 를
# 워밍 때문에 10분 더 살리면 아무 이득 없이 $0.075 를 쓴다. 이 설계가 값을
# 하는 건 작업이 **연달아** 들어올 때이고, 그건 6단계 부하 테스트에서 잰다.
#
# 아직 못 가른 것: IOSchedulingClass=idle 이 옳은지. 워밍과 추론은 대부분
# 같은 데이터(HF 캐시)를 읽고 지연 로딩은 블록을 한 번만 가져오므로, 둘은
# 경합이 아니라 협력일 수 있다. 그렇다면 양보시키는 게 첫 요청을 늦춘다.
# 작업을 돌려 보기 전에는 근거가 없으니 그대로 둔다.
# 백그라운드 워밍의 상한(초). 부팅을 막지 않으니 넉넉하다. 그래도 상한은 둔다 —
# dd 하나가 멈추면 유휴 판정이 날 때까지 디스크를 계속 두드린다.
# 8GB를 80 MB/s 로 읽으면 약 100초다(예전엔 47GB·600초 기준으로 1800을 줬다).
# 그 여섯 배를 준다 — 지연 로딩이 느린 날을 감안해도 남는다.
export EMR_WARM_BG_TIMEOUT=${EMR_WARM_BG_TIMEOUT:-600}

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
# 125 MB/s면 80GB를 다 데우는 데 10분 이상 걸린다.
# 올리면 빨라지지만 125 초과분은 MB/s당 월 $0.04다(250이면 월 $5, 떠 있는
# 시간만큼 비례 과금). 첫 실부팅 로그의 [warm] 줄을 보고 조정한다.
export EMR_VOLUME_THROUGHPUT=${EMR_VOLUME_THROUGHPUT:-250}

# ── SD 가중치 선반입 ────────────────────────────────────────────────────
# 첫 인페인팅의 SD 로드가 인스턴스에서 553초 걸렸다. 같은 가중치를 개발 PC 에서
# 읽으면 5.3초다 — 100배 차이는 역직렬화가 아니라 **디스크 읽기**에서 난다.
# 그래서 가중치 파일을 세그먼트 작업이 도는 동안 미리 통독해 둔다. GPU 에
# 올리지 않으므로 VRAM 과 무관하고, 들키는 비용은 RAM 과 디스크 대역폭뿐이다.
export EMR_SD_WARM=${EMR_SD_WARM:-1}

# 통독한 페이지를 캐시에 남길지 버릴지.
#
#   0 = 남긴다. 페이지 캐시가 따뜻해져서 실제 로드가 메모리에서 끝난다.
#       대신 RAM 2.7GB 를 쓴다 — SAM3D 콜드 RSS 가 15.2GB 인 32GB 머신에서
#       이게 안전한지는 아직 모른다.
#   1 = 버린다(posix_fadvise DONTNEED). 선반입의 효과는 "EBS 블록을 S3 에서
#       끌어와 볼륨에 실체화"하는 것뿐이고 RAM 비용은 0 이다.
#
# **둘 중 뭐가 맞는지는 측정으로 정한다.** 증거가 양쪽으로 갈린다 —
# 실측 11.7MB/s 대 프로비저닝 250MB/s 는 EBS 지연 로딩을 가리키지만,
# sam3d 1230초 조사는 "실체화된 볼륨도 캐시만 비우면 1201초"라고 결론 났다.
# 단계 4 에서 sd_warm.measure() 를 한 번 돌려 정한다(약 $0.12).
# 그 전까지는 0 이다 — 캐시를 남겨서 손해 보는 건 RAM 2.7GB 지만, 버려서
# 틀리면 553초가 그대로 돌아온다.
export EMR_SD_WARM_DROP=${EMR_SD_WARM_DROP:-0}

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
# 부팅 경로 워밍을 없애면서 워밍 항은 빠졌다(예전엔 + EMR_WARM_TIMEOUT 이었다).
#
# 주의: 이 값은 bake-ami.sh 가 emr-guardian.service 의 Environment= 에
# **구워 넣는다.** 이미 구운 AMI에는 옛 값이 들어 있으므로, userdata.sh 가
# 부팅할 때 systemd 드롭인으로 덮어쓴다(EMR_IMAGE_TAG 와 같은 함정이다 —
# 배포 시점 설정이 AMI에 굳은 값에 지면 안 된다).
export GUARDIAN_GRACE_SEC=${GUARDIAN_GRACE_SEC:-$(( 120 + 600 + 300 ))}
export GUARDIAN_FAIL_MIN=${GUARDIAN_FAIL_MIN:-5}       # 리퍼 부재가 이만큼(분) 이어지면 회수

# ── 대화형 세션 알림 ──
# 리퍼는 큐 워커의 상태 파일만 본다. 2단계(가구 고르기 → 빈방 만들기)는 큐를
# 쓰지 않으므로, 사용자가 화면 앞에 앉아 있어도 리퍼 눈에는 "아무 일 없음"이고
# 120초 뒤 인스턴스를 회수한다. 백엔드도 같은 형식의 상태 파일을 쓰게 해서
# 리퍼가 같이 보도록 한다.
#
# **스위치가 하나인 이유.** 양쪽을 따로 켤 수 있게 두면 반드시 한쪽만 켜는 날이
# 온다. 그런데 두 실패가 대칭이 아니다 —
#   백엔드만 켬 : 리퍼가 안 보니 아무 일도 안 일어난다(무해).
#   리퍼만 켬   : 기대하는 파일이 **없으면** 리퍼는 유휴 판정을 영원히 보류한다.
#                 빈 GPU 가 아무도 안 쓰는 채로 계속 돈다(돈이 샌다).
# 그래서 한 변수로 두 쪽을 동시에 렌더한다. 한쪽만 켜는 게 불가능해진다.
export EMR_BACKEND_SESSION=${EMR_BACKEND_SESSION:-1}

# "최근 이만큼 안에 요청이 있었으면 세션이 살아 있다"로 본다. 요청 처리중이라는
# 뜻이 아니다 — 2단계는 클릭과 클릭 사이가 통째로 비어 있는 대화형 루프다.
#
# 이 값이 곧 **세션당 꼬리 요금**이다. 마지막 요청 뒤
#   EMR_BACKEND_BUSY_SEC(여기) + IDLE_EXIT_SEC(900초) 만큼 더 돌고 내려간다.
# 300초면 꼬리 1200초, 600초면 1500초다. 20세션/일 기준 그 차이가 월 $32 쯤 된다
# (docker-compose.aws.yml 의 리퍼 주석에 있는 비용 모델과 같은 환산).
#
# 300초로 잡은 근거: 2단계의 클릭 간격은 초 단위이고(SegmentStep.jsx 는 300ms
# 디바운스), 3·4단계는 큐 워커가 스스로 바쁘다고 보고하므로 이 값과 무관하다.
# 5분을 내리 아무 요청도 없는 구간은 "사람이 자리를 떴다"로 보는 게 맞다.
# 더 줄이려면 프런트가 가벼운 하트비트를 보내는 쪽이 옳다(별도 작업).
export EMR_BACKEND_BUSY_SEC=${EMR_BACKEND_BUSY_SEC:-300}

# ── 리퍼 유휴 기준 (2026-10-07 에 120 → 900) ────────────────────────────
# 여태 이 변수는 **아무 데도 선언되어 있지 않았다.** 쓰는 쪽이 전부
# ${IDLE_EXIT_SEC:-120} 폴백이라 조용히 120초로 돌았고, 그래서 "설정을 바꾸려면
# 어디를 고쳐야 하는지"가 보이지 않았다.
#
# 120초를 고른 근거는 docker-compose.aws.yml 의 리퍼 주석에 있는데, 거기 적힌
# 전제가 "부팅 3분을 어차피 감수한다"였다. 실측은 **390초(6.5분)** 다.
# 유휴 120초 < 부팅 390초면 비율이 거꾸로다 — 사람이 잠깐 손을 놓으면 회수되고,
# 다시 누르면 6분 반을 기다린다. 실제로 2026-10-07 에 그 고리에 걸렸다:
#   13:59 ready → 14:06 회수(유휴 120초) → 14:10 클릭 → 재기동 실패 → 대기
# 게다가 스팟 쿼터가 8 vCPU(= g6.2xlarge 딱 한 대)라 회수 직후 재기동은
# 종료 중인 인스턴스가 자리를 비울 때까지 MaxSpotInstanceCountExceeded 로
# 실패한다. 회수가 잦을수록 이 충돌을 자주 만난다.
#
# 900초면 꼬리는 EMR_BACKEND_BUSY_SEC(300) + 900 = 1200초다. 세션당 +$0.12
# (스팟 $0.5663/h). 하루 20세션을 띄엄띄엄 쓰면 월 +$74 지만, 세션이 15분 안에
# 이어지면 재부팅이 통째로 사라진다 — 부팅 390초도 어차피 과금되는 시간이라
# 재부팅 한 번을 막을 때마다 $0.061 을 돌려받는다. 연속 작업에서는 더 싸다.
#
# 여기를 다시 줄이고 싶어지면 먼저 부팅 시간을 재라. 유휴가 부팅보다 짧아지는
# 순간 같은 고리로 돌아간다.
#
# GUARDIAN_GRACE_SEC(1020초)보다 작아야 한다. 크면 가디언이 멀쩡히 유휴 대기
# 중인 워커를 "고장"으로 보고 죽인다.
export IDLE_EXIT_SEC=${IDLE_EXIT_SEC:-900}

# ── 유저데이터 16KB 한도 ──
# EC2 유저데이터 한도는 base64 **전** 원본 16384 바이트다. userdata.sh 는
# 주석까지 통째로 인스턴스로 올라가므로 주석 한 글자가 그대로 한도를 깎고,
# 한글은 글자당 3바이트다.
#
# 실제로 한 번 넘겼다. 구분선을 `# ── 기동 ─────…` 처럼 74칸까지 늘여 긋던
# 10줄이 장식만 1.9KB 를 먹었고(`─` 가 3바이트다), CloudWatch 로깅 오버레이가
# 들어오면서 렌더 결과가 16,557 바이트가 됐다. 20-launch-template.sh 의
# 사전 점검이 시작 템플릿 생성을 거부한다 — 부팅 때 죽는 것보다는 싸지만,
# 원인이 "주석이 길어서"라는 걸 알아채는 데 시간이 걸린다.
#
# 고친 방법은 구분선 꼬리를 `──` 로 끊은 것뿐이다(의미는 하나도 안 버렸다).
# 그러니 앞으로 긴 근거 주석을 새로 쓸 자리는 **여기**다. config.sh 는
# 인스턴스로 가지 않는다 — 치환된 값만 간다.
#
# 현재 렌더 크기는 20-launch-template.sh 실행 시 출력된다. 15.8KB 쯤이고
# 여유는 600바이트 안팎이다. 여유가 빠듯해지면 주석부터 이쪽으로 옮길 것.

# ── API 서버 (t4g.small 상시 1대) ───────────────────────────────────────
# GPU 워커와 완전히 다른 생물이다. 아래 넷을 구분해 두지 않으면 50-api.sh 가
# GPU 쪽 설정을 잘못 물려받는다.
#
#   GPU 워커                         API 서버
#   ─────────────────────────────    ─────────────────────────────
#   x86_64 (g6/g5)                   arm64 (Graviton, t4g)
#   ASG + 시작 템플릿, 0~1대          run-instances 로 1대 고정
#   AMI 에 전부 구워 넣음(80GB)       기동 시 git clone + docker build
#   인바운드 0개, 키 없음             22(내 IP만) + 8000(공개)
#
# 세 번째 줄이 핵심이다. 워커는 모델 80GB 를 부팅마다 받을 수 없어 AMI 를 굽지만,
# API 는 의존성이 4개(fastapi/uvicorn/python-multipart/boto3)뿐이라 빌드가 1~2분이다.
# AMI 를 굽는 비용(스냅샷 보관 월 $5~6)이 얻는 것보다 크다.
#
# ECR 은 쓰지 않는다. emr-deploy 에 ecr:* 권한이 아예 없고(DescribeRepositories
# 가 AccessDenied), 저장소가 공개라 인스턴스에서 자격증명 없이 clone 이 된다.
# 권한을 넓히지 않고 끝나는 쪽을 택한다.
#
# **GPU AMI 안의 emr/api 이미지는 여기서 못 쓴다.** x86_64 로 구워져 있어서
# t4g 에서는 exec format error 로 죽는다. 같은 Dockerfile 로 arm64 에서 다시
# 빌드하는 것이고, python:3.11-slim 도 네 의존성의 휠도 전부 aarch64 가 있다
# (watchfiles 만 cp310-abi3 인데 abi3 라 3.11 에서 그대로 쓰인다).
# 즉 t4g 위에서 Rust/C 컴파일이 일어나지 않는다 — 2 vCPU 로도 빌드가 끝난다.
export API_NAME=${PROJECT:-emr}-api
export API_SG_NAME=${API_SG_NAME:-${PROJECT:-emr}-api-sg}
export API_PROFILE_NAME=${API_PROFILE_NAME:-${PROJECT:-emr}-api-profile}
export API_INSTANCE_TYPE=${API_INSTANCE_TYPE:-t4g.small}

# t4g.small(2GB) 이 하한이다. micro(1GB) 는 pip 설치 중 터질 수 있고, API 가
# 못 뜨면 capacity.py 가 안 돌아 GPU 가 영영 0대로 남는다 — $2 아끼려다
# 서비스 전체가 멈춘다.
#
# 스팟은 쓰지 않는다. 시간당 $0.0208 → $0.005 로 월 $12 가 줄지만, API 는
# 상시 가동이고 회수당하면 접수 창구가 통째로 닫힌다. 워커는 회수돼도 메시지가
# 큐에 남아 다음 워커가 집어가지만, API 가 없으면 큐에 넣을 사람이 없다.
export API_VOLUME_GB=${API_VOLUME_GB:-8}
export API_PORT=${API_PORT:-8000}

# API → GPU 워커 중계(gpuproxy.py). 워커 백엔드가 듣는 포트이고, 보안그룹에서
# emr-api-sg 에만 연다. 8002/8003(uLayout/Omni3D)은 **열지 않는다** — 백엔드가
# 컨테이너 네트워크 안에서 부르는 포트지 바깥에서 부를 포트가 아니다.
export GPU_PORT=${GPU_PORT:-8001}

# /api/prewarm 의 IP 당 호출 간격. 인증이 없는 엔드포인트가 ASG 를 건드리므로
# 들어오는 쪽에서도 한 번 센다. capacity.py 의 30초 창은 전역이라 호출 자체를
# 막지는 못한다 — 막히는 건 AWS 호출이지 이 서버의 일이 아니다.
export PREWARM_RATE=${PREWARM_RATE:-30}

# AMI/서브넷은 비워 두면 50-api.sh 가 조회해서 채운다. ssm:GetParameter 가
# AccessDenied 라 AL2023 공식 파라미터 경로를 못 쓴다 — describe-images 로
# 이름 패턴을 직접 뒤진다.
export API_AMI_PATTERN=${API_AMI_PATTERN:-al2023-ami-2023.*-kernel-6.1-arm64}
export API_AMI_ID=${API_AMI_ID:-}
export API_SUBNET_ID=${API_SUBNET_ID:-}

# 워커와 달리 들어가 볼 수 있어야 한다. 워커는 실패하면 트랩이 회수해 버리니
# 들어갈 일이 없지만, API 는 실패해도 살아 있어야 고칠 수 있다(아래 참고).
# 빌더 키를 재사용한다 — ~/.ssh/emr-builder-key.pem 이 이미 있다.
export API_KEY_NAME=${API_KEY_NAME:-emr-builder-key}
export API_SSH_CIDR=${API_SSH_CIDR:-}     # 비면 checkip.amazonaws.com 으로 /32

export API_REPO_URL=${API_REPO_URL:-https://github.com/simu1231/Empty_My_Room.git}
export API_GIT_REF=${API_GIT_REF:-main}

# **배포 전에 반드시 좁힌다.** api/main.py 의 기본값이 "*" 라서 지금은 아무
# 사이트나 이 API 를 부를 수 있다. 프런트 도메인이 정해지기 전이라 임시로 둔다.
# 좁히는 자리는 여기 한 곳이고, 값은 .env 를 통해 컨테이너로 들어간다.
export CORS_ORIGINS=${CORS_ORIGINS:-*}

# capacity.py 의 중복 호출 억제 간격. 사진 1장이 7건(sam3d 3 + scene 4)을
# 연달아 만들어도 SetDesiredCapacity 는 한 번만 부른다.
export CAPACITY_NUDGE_INTERVAL=${CAPACITY_NUDGE_INTERVAL:-30}

# ── 프런트 배포 (단계 6: S3 + CloudFront) ───────────────────────────────
# 정적 사이트 버킷. 잡 버킷과 **나눠 둔다** — 잡 버킷은 CloudFront 가 읽을
# 이유가 없고, 수명주기 규칙(input/ result/ 7일)도 사이트 파일에 걸리면 안 된다.
# 이름 규칙은 S3_BUCKET 과 같은 계정 해시를 재사용한다. 계정 ID 를 저장소에
# 남기지 않으면서 전역 유일성을 얻는 같은 이유다.
# S3_BUCKET 이 추측값(EMR_BUCKET_GUESSED=1)이면 접미사도 가짜다 — emr-jobs 에서
# ${S3_BUCKET##*-} 를 떼면 "jobs" 가 나와 emr-site-jobs 라는 엉뚱한 버킷을
# 조용히 만들게 된다. 잡 버킷은 없으면 NoSuchBucket 으로라도 터지지만, 사이트
# 버킷은 **새로 만드는 것**이라 안 터지고 그냥 잘못된 이름이 생긴다. 막는다.
if [ -z "${EMR_SITE_BUCKET:-}" ]; then
  if [ "${EMR_BUCKET_GUESSED:-0}" = "1" ]; then
    echo "⚠ S3_BUCKET 이 추측값이라 EMR_SITE_BUCKET 을 정할 수 없다 ($S3_BUCKET)" >&2
    echo "  aws CLI 와 자격증명을 확인하거나 EMR_SITE_BUCKET 을 직접 지정하라" >&2
  else
    export EMR_SITE_BUCKET="emr-site-${S3_BUCKET##*-}"
  fi
fi

# CloudFront 는 전역 서비스라 리전이 없다. 배포를 찾는 열쇠로 Comment 를 쓴다
# — ListDistributions 는 태그를 돌려주지 않아서, 태그로 찾으려면 배포마다
# ListTagsForResource 를 또 불러야 한다. Comment 는 목록에 바로 들어 있다.
export EMR_CF_COMMENT=${EMR_CF_COMMENT:-${PROJECT:-emr}-site}
export EMR_CF_OAC_NAME=${EMR_CF_OAC_NAME:-${PROJECT:-emr}-site-oac}

# AWS 관리형 정책 ID. 이름이 아니라 ID 로 박아 둔다 — 전 계정 공통 고정값이고,
# 이름으로 찾으려면 list-cache-policies 권한이 매번 필요하다.
#   CachingOptimized            정적 파일용. 압축 켜고 쿼리스트링 무시.
#   CachingDisabled             API 용. 캐시 금지.
#   AllViewerExceptHostHeader   API 용. Host 를 뺀 나머지를 그대로 넘긴다.
#     Host 를 빼는 이유: 그대로 넘기면 오리진이 CloudFront 도메인을 Host 로
#     받는데, EC2 오리진은 자기 이름을 모르므로 맞춰줄 게 없다. 빼면
#     CloudFront 가 오리진 도메인으로 채워 준다.
export EMR_CF_CACHE_OPTIMIZED=${EMR_CF_CACHE_OPTIMIZED:-658327ea-f89d-4fab-a63d-7e88639e58f6}
export EMR_CF_CACHE_DISABLED=${EMR_CF_CACHE_DISABLED:-4135ea2d-6df8-44a3-9df3-4b5a84be39ad}
export EMR_CF_ORP_ALLVIEWER_NOHOST=${EMR_CF_ORP_ALLVIEWER_NOHOST:-b689b0a8-53d0-40ab-baf2-68738e2966ac}

# API 오리진. CloudFront 오리진은 IP 를 받지 않고 도메인만 받는다. EC2 의
# 공개 DNS 는 공인 IP 에서 파생되므로 인스턴스를 교체하면 주소가 바뀐다 —
# 그래서 EIP 를 붙여 고정한다. 비워 두면 60-cloudfront.sh 가 API 인스턴스에서
# 읽어 채운다.
export EMR_API_ORIGIN_DNS=${EMR_API_ORIGIN_DNS:-}
export EMR_CF_DIST_ID=${EMR_CF_DIST_ID:-}

# CloudFront 엣지가 오리진을 부를 때 쓰는 IP 대역. AWS 가 관리하므로 엣지가
# 늘거나 줄어도 우리가 손댈 게 없다. API 의 8000 을 이것만 허용하면 CloudFront
# 를 우회해 /api/prewarm 을 직접 때려 **GPU 를 깨우는** 길이 막힌다.
#
# IPv6 판(pl-07ac407da2b364d6c, 35개)은 쓰지 않는다. 인스턴스에도 서브넷에도
# IPv6 가 없어 쓸 일이 없고, 둘 다 넣으면 46+35+1=82 로 보안그룹 규칙
# 할당량(L-0EA8095F, 60)을 넘겨 거부된다.
export EMR_CF_PREFIX_LIST=${EMR_CF_PREFIX_LIST:-pl-22a6434b}
