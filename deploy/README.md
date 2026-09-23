# 배포 구성 (AWS, 스케일투제로 + 큐)

## 왜 이런 구조인가

GPU 인스턴스는 시간당 비싸다. 사용자가 없을 때 0대까지 줄이려면,
0대인 동안에도 요청을 받아줄 **GPU가 필요 없는 상시 서버**가 있어야 한다.
그래서 접수(API)와 추론(Worker)을 분리한다.

```
React ──► API 서버 ──► SQS ──► GPU Worker ──► S3
         (CPU 상시)            (0 ~ N대)
              │                    │
              └──── DynamoDB ◄─────┘   (작업 상태)
```

큐의 역할은 3가지다. "GPU가 꺼진 동안 일을 담아두는 것"은 그중 하나일 뿐이다.

1. **HTTP 타임아웃 회피** — 50초 작업을 커넥션 붙잡고 기다리지 않는다
2. **VRAM 보호** — 워커가 한 번에 1건만 꺼내므로 동시 요청에도 OOM이 안 난다
3. **유실 방지** — 워커가 죽어도 메시지가 남아 재시도된다 (스팟 인스턴스 사용의 전제)

## 단계

- [x] **1. 큐 배관 + 로컬 검증** ← 현재. GPU 없이 가짜 워커로 검증
- [ ] 2. 프런트엔드 비동기 전환 (동기 `await fetch` → 접수 + 폴링)
- [ ] 3. 실제 모델 이식 + GPU 이미지 빌드
- [ ] 4. ASG 오토스케일링 + 스케일투제로
- [ ] 5. AWS 배포 (Terraform)
- [ ] 6. 동시 요청 부하 테스트

## 1단계 실행법

사전 준비: WSL 안에 Docker Engine 설치 (Docker Desktop 아님 — 아래 주의 참고)

```bash
docker compose -f deploy/docker-compose.yml up --build
```

검증:

```bash
# 작업 접수 → job_id 즉시 반환되어야 한다
curl -X POST http://localhost:8000/api/jobs \
  -F "image=@/tmp/test_sofa.png" -F "job_type=sam3d_mesh"

# 상태 조회 → queued → running → done 으로 바뀌어야 한다
curl http://localhost:8000/api/jobs/<job_id>
```

## 주의: Docker Desktop을 쓰지 말 것

Docker Desktop은 이미지를 Windows C드라이브에 쌓는다. GPU 이미지가 개당 8GB급이라
금방 C가 가득 차고, 그러면 WSL2의 GPU 할당 자체가 실패한다
(원인 분석: 메모리 `project-wsl-gpu-oom-cdrive` 참고).

WSL 안에 Docker Engine을 직접 설치하면 이미지가 `/var/lib/docker`
= `/dev/sdd` (785GB 여유)에 저장되어 이 문제가 없다.
