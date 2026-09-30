#!/bin/bash
# 인스턴스가 뜰 때마다 실행된다. 하는 일은 하나 — 컴포즈 스택을 올린다.
#
# 여기서 모델을 내려받거나 conda 환경을 설치하지 않는다. AMI에 이미 들어 있어야
# 한다(47GB). 부팅할 때마다 받으면 콜드스타트가 수십 분이 되고, 그러면 스케일투제로의
# 이점이 전부 사라진다.
set -euxo pipefail
exec > >(tee /var/log/emr-userdata.log | logger -t emr-userdata) 2>&1

REPO=/home/tmvlem5671/Empty_My_Room

# EBS 스냅샷에서 복원한 볼륨은 블록을 처음 읽을 때 S3에서 끌어온다(지연 로딩).
# 모델 가중치 31GB를 추론 중에 처음 읽으면 첫 작업이 몇 분씩 걸린다. 미리 훑어서
# 그 비용을 부팅 시간으로 옮긴다 — 어차피 기다리는 시간이면 사용자 요청 앞이 낫다.
# fio가 없으면 dd로 대체한다.
if command -v fio >/dev/null; then
  fio --filename=/dev/nvme1n1 --rw=read --bs=1M --iodepth=32 --ioengine=libaio \
      --direct=1 --name=warm --runtime=180 --time_based=0 || true
fi

cd "$REPO"
# 큐/버킷 이름은 환경변수로만 주입한다 — 코드에는 기본값만 있고, 어느 큐를 보는지는
# 배포가 정한다. 이 덕에 나중에 sam3d와 scene을 다른 인스턴스로 쪼갤 때 코드를
# 한 줄도 안 고치고 컴포즈만 나누면 된다.
cat > deploy/.env <<ENV
AWS_ENDPOINT_URL=
AWS_REGION=__REGION__
S3_BUCKET=__S3_BUCKET__
DDB_TABLE=__DDB_TABLE__
Q_SAM3D=__Q_SAM3D__
Q_SCENE=__Q_SCENE__
IDLE_EXIT_SEC=__IDLE_EXIT_SEC__
DRY_RUN=0
ENV

docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml \
               -f deploy/docker-compose.aws.yml up -d
