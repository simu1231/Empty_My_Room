# GPU 컨테이너 공용 이미지 — SAM3D 워커 / uLayout 사이드카 / Omni3D 사이드카가 모두 이걸 쓴다.
#
# 왜 하나로 충분한가?
#   conda 환경(32GB) · 모델 소스 · 가중치(31GB)를 전부 볼륨으로 마운트하기 때문에,
#   세 컨테이너의 차이는 "어느 python으로 어떤 스크립트를 실행하는가"뿐이다.
#   그건 빌드 시점이 아니라 런타임 설정이므로 이미지를 나눌 이유가 없다.
#
# 왜 환경과 가중치를 이미지에 굽지 않는가?
#   합치면 60GB가 넘고, 스케일투제로는 인스턴스가 매번 새로 뜬다. 그때마다 60GB를
#   내려받으면 콜드스타트가 수십 분이 된다. 우리가 없애려던 지연을 다시 만드는 셈이다.
#   운영에서는 이 볼륨들을 EBS 스냅샷으로 붙인다(스냅샷 연결은 수십 초).
#
# 왜 컨테이너 안 경로가 호스트 절대경로와 같은가?
#   conda 환경은 prefix 절대경로가 스크립트와 .pth 파일에 박혀 있고, sam3d_objects /
#   pytorch3d 등은 editable 설치라 소스 위치도 절대경로로 기록돼 있다. 다른 경로에
#   마운트하면 conda-pack 같은 재배치 작업이 따로 필요해진다. 경로를 맞추는 쪽이 훨씬 싸다.

FROM ubuntu:24.04

# nvidia-container-toolkit이 이 두 값을 보고 호스트 GPU 드라이버를 주입한다.
# CUDA 런타임 자체는 conda 환경 안의 torch가 들고 있으므로 이미지에 넣지 않는다.
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

# conda 환경이 자기 안에 안 넣고 OS에 기대는 것들.
# libGL/libglib: opencv, libgomp: OpenMP, libusb: 일부 torch 확장.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libusb-1.0-0 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

# 마운트한 conda 환경에는 boto3가 없다. 19GB짜리 환경을 건드리는 대신 별도 디렉터리에
# 두고 PYTHONPATH로 얹는다.
#   --no-deps 인 이유: boto3가 끌고 오는 urllib3 / python-dateutil / six 는 이미
#   conda 환경에 있다. 그것까지 여기 깔면 PYTHONPATH가 site-packages보다 우선하므로
#   환경이 검증해둔 버전을 가려버린다. 실제로 없는 4개만 넣는다.
RUN pip install --no-cache-dir --break-system-packages \
        --no-deps --target /opt/worker-deps \
        boto3 botocore jmespath s3transfer

# 호스트와 같은 UID로 돌려야 마운트된 파일을 읽고, __pycache__를 root 소유로
# 만들어 호스트를 더럽히지 않는다. ubuntu:24.04는 uid 1000을 이미 쓰고 있어 비운다.
ARG APP_USER=tmvlem5671
ARG APP_UID=1000
RUN userdel -r ubuntu 2>/dev/null || true; \
    useradd -m -u ${APP_UID} -s /bin/bash ${APP_USER}

COPY --chown=${APP_UID} worker.py /app/worker.py

# 마운트된 소스 디렉터리에 .pyc를 흘리지 않는다.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/worker-deps

USER ${APP_USER}
WORKDIR /app

# 기본값은 SAM3D 워커. 사이드카는 compose에서 command로 덮어쓴다.
CMD ["/home/tmvlem5671/miniconda3/envs/sam3d/bin/python", "-u", "/app/worker.py"]
