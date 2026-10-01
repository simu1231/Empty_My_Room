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

# ── 모델 루트 ────────────────────────────────────────────────────────────
# conda 환경 · 소스 · 가중치가 호스트에 놓인 위치. 컨테이너 안에서도 같은
# 경로로 마운트한다(위 "왜 경로가 같은가" 참고).
#
# 예전에는 여기가 /home/tmvlem5671 로 **박혀** 있었다. 두 가지가 깨졌다:
#   - EC2 기본 사용자는 ubuntu라 그 경로가 없다 → 배포 불가
#   - 이 PC를 같이 쓰는 다른 두 사람은 이미지를 빌드해도 못 쓴다
# 그래서 빌드 인자로 뺐다. 로컬은 compose가 ${HOME}을 넣어 주므로 전과 같고,
# AWS는 config.sh의 EMR_ROOT(/opt/emr)가 들어간다.
ARG EMR_ROOT=/opt/emr

# 호스트와 같은 UID로 돌려야 마운트된 파일을 읽고, __pycache__를 root 소유로
# 만들어 호스트를 더럽히지 않는다. ubuntu:24.04는 uid 1000을 이미 쓰고 있어 비운다.
# 계정 **이름**은 아무 의미가 없다(호스트 파일은 uid로 매칭된다). 그래서
# 사람 이름 대신 emr로 두고, 홈만 EMR_ROOT로 맞춘다.
ARG APP_UID=1000
RUN userdel -r ubuntu 2>/dev/null || true; \
    useradd -M -d ${EMR_ROOT} -u ${APP_UID} -s /bin/bash emr

# EMR_ROOT 와 그 아래 .cache 를 이미지 안에 미리, 앱 사용자 소유로 만들어 둔다.
# 안 만들면 docker가 .cache/huggingface 같은 마운트 지점의 없는 부모를
# **root 소유로** 자동 생성하고, 그러면 uid 1000이 그 안에 새 캐시 디렉터리를
# 못 만든다. 실제로 NVIDIA Warp가 ~/.cache/warp 생성에 실패해 첫 작업만
# 죽는 현상이 있었다(두 번째부터는 우회 경로를 타서 성공 — 그래서 더 위험하다).
RUN install -d -o ${APP_UID} -g ${APP_UID} ${EMR_ROOT} ${EMR_ROOT}/.cache

COPY --chown=${APP_UID} worker.py /app/worker.py

# 워커와 리퍼가 상태를 주고받는 디렉터리. 이미지 안에 미리 만들어야 한다.
# 도커는 빈 named volume을 마운트할 때 **이미지 쪽 경로의 소유권/권한을 복사**하므로,
# 여기서 안 만들면 볼륨이 root 소유로 생기고 uid 1000인 이 컨테이너가 못 쓴다.
# 0777인 이유는 CPU 워커(root)와 GPU 워커(uid 1000)가 같은 볼륨을 공유하기 때문이다.
RUN install -d -m 0777 /state

# 마운트된 소스 디렉터리에 .pyc를 흘리지 않는다.
# EMR_ROOT를 ENV로도 남긴다 — CMD와 worker.py가 런타임에 이 값을 읽는다.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/worker-deps \
    EMR_ROOT=${EMR_ROOT} \
    HOME=${EMR_ROOT}

USER emr
WORKDIR /app

# 기본값은 SAM3D 워커. 사이드카는 compose에서 command로 덮어쓴다.
# exec 형식(JSON 배열)은 변수를 치환하지 않으므로 sh -c 를 거친다. exec 를 붙여
# python이 PID 1을 이어받게 한다 — 안 그러면 SIGTERM이 전달되지 않아 컨테이너
# 종료가 10초 타임아웃 뒤 SIGKILL이 되고, 작업 중이던 워커가 정리할 틈이 없다.
CMD ["sh", "-c", "exec \"$EMR_ROOT/miniconda3/envs/sam3d/bin/python\" -u /app/worker.py"]
