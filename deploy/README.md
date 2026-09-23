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

- [x] 1. 큐 배관 + 로컬 검증 (GPU 없이 가짜 워커로 검증)
- [x] **2. 프런트엔드 비동기 전환** ← 현재
- [ ] 3. 실제 모델 이식 + GPU 이미지 빌드
- [ ] 4. ASG 오토스케일링 + 스케일투제로
- [ ] 5. AWS 배포 (Terraform)
- [ ] 6. 동시 요청 부하 테스트

## 2단계에서 정한 것

**대화형 / 배치 분리.** 모든 호출을 큐에 넣으면 안 된다. SAM2 마스크·LaMa
제거·가구 추출은 1초 안에 끝나고 한 세션에서 수십 번 불린다. 이걸 5분
콜드스타트 뒤에 두면 서비스를 쓸 수 없다. 큐를 타는 것은 GPU 배치 작업 3개뿐:

| 호출 | 경로 | 이유 |
|---|---|---|
| SAM2 마스크 / LaMa 제거 / 가구 추출 / 색상·텍스처 | 상시 서버 직접 호출 | 1초 미만, 호출 빈도 높음 |
| SAM3D 메쉬 (50초) | 큐 `emr-sam3d` | GPU 점유 김 |
| uLayout 방 치수, Omni3D 크기 | 큐 `emr-scene` | GPU 필요 |

**전환 스위치.** `VITE_USE_JOB_QUEUE=1`이면 큐, 비워두면 기존 동기 호출.
호출부 코드는 동일하다(`callModel()`). 3단계에서 워커에 실제 모델이 들어가기
전까지 로컬 개발이 깨지지 않게 하기 위한 것이다.

**계약.** 워커가 S3에 올리는 결과 JSON은 기존 동기 API의 응답과 같은 모양이다.
그래서 결과를 받은 뒤의 프런트엔드 코드는 한 줄도 바뀌지 않았다.

**S3 버킷 CORS는 필수다.** 브라우저가 presigned URL로 결과를 직접 받기 때문에
버킷에 CORS가 없으면 응답이 버려진다. curl로 테스트하면 200이 나와서
정상처럼 보이므로 반드시 브라우저나 `Origin` 헤더로 확인할 것.

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
