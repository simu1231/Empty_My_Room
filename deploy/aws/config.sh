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
