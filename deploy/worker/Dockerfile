# CPU 워커 이미지 — scene 큐(uLayout·Omni3D) 담당.
#
# 이 워커는 모델을 직접 안 돌린다. 큐에서 작업을 꺼내 사이드카에 HTTP로 넘기고
# 결과를 S3에 올릴 뿐이라 GPU도 torch도 필요 없다. 그래서 이미지가 100MB대다.
#
# MOCK 여부는 빌드가 아니라 환경변수(MOCK=1)로 정한다. 1단계에서 쓰던
# Dockerfile.mock 과 내용이 같았기 때문에 이미지를 나눌 이유가 없다.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py idle_reaper.py jobspec.py ./

# 워커와 리퍼가 상태를 주고받는 디렉터리(gpu/Dockerfile과 같은 이유로 0777).
RUN install -d -m 0777 /state

ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "worker.py"]
