# CPU 전용. 상시 가동이라 이미지를 최대한 가볍게 유지한다.
# torch를 절대 넣지 않는다 — 넣는 순간 이미지가 5GB로 불어나고
# 콜드스타트와 월 비용이 같이 뛴다.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py capacity.py ./

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
