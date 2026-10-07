# CPU 전용. 상시 가동이라 이미지를 최대한 가볍게 유지한다.
# torch를 절대 넣지 않는다 — 넣는 순간 이미지가 5GB로 불어나고
# 콜드스타트와 월 비용이 같이 뛴다.
FROM python:3.11-slim

# 빌드 컨텍스트는 deploy/ 다(docker-compose.yml 의 api.build 참고).
# api/ 로 좁히면 worker/jobspec.py 를 가져올 수 없어서 검증 규칙이 갈라진다.
WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 파일을 하나씩 적는다. api/ 통째로 복사하면 __pycache__ 나 로컬 실험 파일이
# 같이 들어간다. 대신 **새 모듈을 만들 때 여기 추가하는 걸 잊으면** 이미지에
# 안 들어가고, 컨테이너가 ImportError 로 죽는다.
COPY api/main.py api/capacity.py api/gpuproxy.py api/colors.py ./
# 워커와 **같은** 검증 코드. 복사본이 아니라 같은 파일을 넣는다.
COPY worker/jobspec.py ./

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
