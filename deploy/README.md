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
- [x] 2. 프런트엔드 비동기 전환
- [x] **3. 실제 모델 이식 + GPU 이미지 빌드** ← 현재
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

## 3단계에서 정한 것

**추론 코드를 라우터에서 떼어냈다.** `sam3d/backend/routers/sam3d.py`에 있던
추론 로직을 `sam3d/backend/services/sam3d_runner.py`로 옮겼다. 상시 서버(동기
HTTP)와 큐 워커가 **같은 코드**를 쓰게 하기 위해서다. 복사해두면 한쪽만 고치는
사고가 반드시 난다. 라우터는 340줄에서 57줄짜리 얇은 껍데기가 됐다.

옮기면서 숨은 결합 하나를 발견해 같이 고쳤다. `sam3d_objects`는
`LIDRA_SKIP_INIT` 환경변수가 있어야 import되는데 그걸 `main.py`가 설정하고
있었다. 워커는 `main.py`를 거치지 않으므로 runner가 직접 설정하게 바꿨다.

**워커의 세 갈래.** job_type별로 성격이 완전히 다르다.

| job_type | 처리 방식 | 이유 |
|---|---|---|
| `sam3d_mesh` | 워커 프로세스 안에서 직접 추론 | 무거운 GPU 작업, 파이프라인 캐시가 이득 |
| `room_layout` | uLayout 사이드카로 HTTP 전달 | conda 환경이 달라 한 프로세스에 못 올림 |
| `omni3d` | Omni3D 사이드카로 HTTP 전달 | 위와 같음 |

**GPU 이미지는 하나뿐이다.** 컨테이너는 셋인데 Dockerfile은 하나다. 환경과
가중치를 전부 마운트하므로 셋의 차이가 "어느 python으로 뭘 실행하는가"밖에
없기 때문이다. 자세한 내용은 [gpu/README.md](gpu/README.md).

### 3단계 실측값

컨테이너 안의 실제 모델로 큐를 통과시킨 결과다(RTX 4090 24GB, LocalStack).

| 작업 | 소요 | 비고 |
|---|---|---|
| `sam3d_mesh` (콜드) | 47~48초 | 파이프라인 로드 24.3초 + 추론 22~30초 |
| `sam3d_mesh` (웜) | 25~30초 | 파이프라인 캐시 적중 |
| `room_layout` | 3.0초 | |
| `omni3d` | 1.0초 | |

추론 시간이 22~30초로 흔들리는 건 부하가 아니라 **seed 탐색 결과**다.
뽑힌 메쉬 크기가 매번 달라서(버텍스 2,423~19,456) 후처리 시간이 따라 변한다.
벤치마크로 쓸 때는 한 번 값이 아니라 범위로 봐야 한다.

네이티브 conda 프로세스로 쟀던 값(로드 35.9초)보다 컨테이너 쪽 로드가
오히려 빠른데, 컨테이너 오버헤드가 음수일 리는 없고 두 번째 측정이라
호스트 페이지 캐시가 더워진 영향으로 본다. **컨테이너화로 인한 추론 성능
손실은 관측되지 않았다**가 여기서 말할 수 있는 전부다.

**콜드/웜 차이 약 24초(=파이프라인 로드)가 4단계 설계의 핵심 숫자다.**
인스턴스를 0대로 줄이면 매번 이 24초를 다시 낸다. 여기에 EC2 기동 시간과
이미지 풀 시간까지 더한 것이 사용자가 느낄 콜드스타트다. 이 비용과 유휴
GPU 비용을 어디서 맞바꿀지가 다음 단계다.

결과 JSON이 기존 동기 응답과 같은 계약인지도 확인했다 —
`success` / `type` / `mesh{vertices,faces,colors}` / `sam3d_size_m`.

### 3단계에서 실제로 걸린 것 — 전부 컨테이너에서만 나는 문제였다

네이티브 conda 프로세스로는 셋 다 잘 돌았는데 컨테이너에서 새로 터졌다.
"로컬에서 되니까 됐다"가 왜 위험한지 보여주는 사례라 남겨둔다.

**1. `ModuleNotFoundError: No module named 'detectron2'`**
omni3d 환경의 detectron2/pytorch3d가 editable 설치라 site-packages 바깥
소스를 가리키는데 그걸 마운트하지 않았다. 찾는 방법은
[gpu/README.md](gpu/README.md#editable-설치를-빠짐없이-마운트하는-법).

**2. 첫 작업만 실패하고 두 번째부터 성공**
`~/.cache/huggingface`만 마운트하니 docker가 부모 `~/.cache`를 root 소유로
만들었고, uid 1000이 `~/.cache/warp`를 못 만들었다. hydra가 이걸
`Error locating target ...`으로 덮어써서 원인이 안 보였다 — 진짜 원인
(`PermissionError`)은 그 위 스택에 있었다. **스케일투제로에서는 콜드스타트
마다 첫 요청이 실패한다는 뜻**이라 지금 잡은 게 다행이다.

**3. `CUDA driver error: unknown error`**
WSL2의 `dxg` 드라이버는 공유 GPU 리소스마다 fd를 하나씩 쓴다. Docker 기본
소프트 한도가 1024인데 파이프라인을 캐시해두면 정상 상태에서 이미 ~1000을
쓴다. CUDA 메시지로는 절대 모르고, `dmesg`에 답이 있었다.

```
dxgk: get_unused_fd_flags failed: ffffffe8   ← 0xffffffe8 = -24 = EMFILE
```

fd 수를 작업마다 재보니 949 → 1021 → 1005로 **누적되지 않았다.** 누수가
아니라 정상 작업 세트가 하필 기본값에 걸린 것이라, 한도 상향이 맞는 처방이다.
(누수였다면 한도를 올려도 터지는 시점만 미뤄진다 — 이 구분을 꼭 하고 넘어갈 것.)

**덤으로 재시도 설계가 검증됐다.** 3번으로 실패한 작업은 메시지를 지우지
않았으므로 SQS가 다시 내려줬고, 고친 뒤 자동으로 성공해 `done`이 됐다.
최종 상태는 13건 전부 `done`, 큐와 DLQ 모두 0이다.

## 3단계 실행법

```bash
# GPU 스택 (오버라이드로 기본 compose 위에 덮어쓴다)
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml up --build
```

전제: `nvidia-container-toolkit`. 미설치면 컨테이너가 GPU를 못 본다.

이 패키지는 **Ubuntu 기본 저장소에 없다.** NVIDIA 저장소를 먼저 등록해야
`Unable to locate package`가 안 난다. WSL 안에서 root로:

```bash
# 관리자가 아닌 PowerShell에서:  wsl -d Ubuntu -u root
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get update
apt-get install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker   # /etc/docker/daemon.json에 nvidia 런타임 등록
service docker restart                          # 이걸 빼면 Docker가 런타임을 모른다
```

확인:

```bash
docker run --rm --gpus all ubuntu:24.04 nvidia-smi
```

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
