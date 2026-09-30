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
export S3_BUCKET=emr-jobs
export DDB_TABLE=emr-jobs

export LT_NAME=${PROJECT}-gpu-worker            # 시작 템플릿
export ASG_SPOT=${PROJECT}-gpu-spot             # 평소 쓰는 스팟 그룹
export ASG_OD=${PROJECT}-gpu-ondemand           # 스팟이 안 뜰 때만 쓰는 폴백 그룹
export ROLE_NAME=${PROJECT}-gpu-worker-role
export PROFILE_NAME=${PROJECT}-gpu-worker-profile

# ── 용량 ────────────────────────────────────────────────────────────────
# ①(세 컨테이너 한 대) 구성이라 인스턴스 1대 = sam3d 1건 + scene 1건 동시 처리다.
export MAX_SPOT=3       # 스팟 그룹 최대
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
# 2xlarge는 GPU가 같고 CPU/RAM만 넉넉한 것이라 우리 워크로드에 그대로 쓸 수 있다.
# g6e(L40S 48GB)는 비싸지만, 못 뜨는 것보단 나아서 맨 뒤에 둔다.
export INSTANCE_TYPES="g6.xlarge g6.2xlarge g5.xlarge g5.2xlarge g6e.xlarge"

# ── AMI / 이미지 ────────────────────────────────────────────────────────
# 인스턴스 안에서 저장소가 어디에 있는지. userdata가 여기로 cd 한다.
# 이전에는 개발 PC의 홈 경로(/home/tmvlem5671/...)가 박혀 있었다 — EC2 기본
# 사용자는 ubuntu라서 그 경로가 없고, cd 실패 → userdata 죽음 → 리퍼도 안 뜸
# → 인스턴스가 일 없이 영원히 과금되는 경로로 직행했다.
export EMR_REPO_DIR=${EMR_REPO_DIR:-/opt/emr/Empty_My_Room}

# AMI에 구워 넣은 도커 이미지의 태그. bake-images.sh가 git SHA로 붙인다.
# userdata는 부팅 때 이 태그가 실제로 있는지 확인하고, 없으면 빌드를 시도하지
# 않고 크게 실패한다 — 부팅 중 빌드는 콜드스타트를 수십 분으로 늘린다.
export EMR_IMAGE_TAG=${EMR_IMAGE_TAG:-$(git -C "$(dirname "${BASH_SOURCE[0]}")/../.." rev-parse --short HEAD 2>/dev/null || echo dev)}

# 부팅 때 미리 읽어둘 디렉터리(스냅샷 지연 로딩 해소용). 공백으로 구분.
# 장치명(/dev/nvme1n1)을 찍지 않는다 — 장치 번호는 볼륨 구성에 따라 바뀌고,
# 틀려도 조용히 넘어가서 워밍이 아예 안 된 걸 모른다. 파일 단위로 읽으면
# 장치와 무관하고, 필요한 가중치만 읽어서 47GB 전체보다 빠르다.
export EMR_WARM_DIRS=${EMR_WARM_DIRS:-"$EMR_REPO_DIR/sam-3d-objects $EMR_REPO_DIR/uLayout $EMR_REPO_DIR/omni3d"}
export EMR_WARM_TIMEOUT=${EMR_WARM_TIMEOUT:-600}   # 워밍에 쓸 최대 시간(초)

# ── 가디언(요금 폭주 차단) ──────────────────────────────────────────────
# 리퍼는 컴포즈 스택 안의 컨테이너다. 그래서 컴포즈가 안 뜨면 리퍼도 없고,
# ASG 헬스체크는 EC2(켜져 있는지)만 보므로 인스턴스가 일 없이 계속 과금된다.
# 가디언은 호스트에서 systemd 타이머로 돌며 "리퍼가 살아있나"만 본다.
export GUARDIAN_GRACE_SEC=${GUARDIAN_GRACE_SEC:-900}   # 부팅 후 이 시간은 봐준다
export GUARDIAN_FAIL_MIN=${GUARDIAN_FAIL_MIN:-5}       # 리퍼 부재가 이만큼(분) 이어지면 회수
