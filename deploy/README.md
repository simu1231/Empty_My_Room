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
- [x] 3. 실제 모델 이식 + GPU 이미지 빌드
- [x] **4. ASG 오토스케일링 + 스케일투제로** ← 현재
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

검증은 종단 테스트 스크립트로 한다(입력 이미지를 직접 만들므로 준비물이 없다).

```bash
~/miniconda3/envs/sam3d/bin/python deploy/test_e2e.py
```

접수/조회만 손으로 확인하려면:

```bash
# 작업 접수 → job_id 즉시 반환되어야 한다
curl -X POST http://localhost:8000/api/jobs \
  -F "image=@<아무 이미지>" -F "job_type=sam3d_mesh"

# 상태 조회 → queued → running → done 으로 바뀌어야 한다
curl http://localhost:8000/api/jobs/<job_id>
```

## 주의: Docker Desktop을 쓰지 말 것

Docker Desktop은 이미지를 Windows C드라이브에 쌓는다. GPU 이미지가 개당 8GB급이라
금방 C가 가득 차고, 그러면 WSL2의 GPU 할당 자체가 실패한다
(원인 분석: 메모리 `project-wsl-gpu-oom-cdrive` 참고).

WSL 안에 Docker Engine을 직접 설치하면 이미지가 `/var/lib/docker`
= `/dev/sdd` (785GB 여유)에 저장되어 이 문제가 없다.

## 4단계 준비: 어느 인스턴스에 무엇을 올릴지 실측으로 정하기

### 산술로 판단하면 틀린다

처음에는 컨테이너별 VRAM을 따로 재서 더했다.

| 컨테이너 | 단독 측정(RTX 4090) |
|---|---|
| worker-sam3d | 19,640 MiB (유휴) / 22,347 MiB (피크) |
| ulayout | 1,196 MiB |
| omni3d | 685 MiB |
| **합계** | **24,228 MiB** |

g5.xlarge의 A10G는 **23,028 MiB**다(24GB로 광고되지만 nvidia-smi 실제값은 그보다
1,536 MiB 적고, 개발용 4090의 24,564보다도 적다). 합계가 한도를 넘으니 "세 컨테이너를
한 대에 올리는 건 불가능"이라고 판단했는데 **이 판단이 틀렸다.**

PyTorch 캐싱 할당자는 고정량을 쓰는 게 아니라 **남은 공간에 맞춰 캐시를 줄인다.**
단독으로 두면 22.3GB까지 욕심껏 캐시를 불리지만 좁히면 그만큼 덜 쥔다. 단독 측정치의
합은 "필요량"이 아니라 "혼자일 때 쓰고 싶어하는 양"이다.

### 목표 카드의 '여유'를 재현해서 실제로 돌려본다

`deploy/simulate_a10g.py`가 로컬 4090에서 g5.xlarge와 **같은 여유 VRAM**을 만든다.

```bash
python deploy/simulate_a10g.py          # 점유하고 대기 (Ctrl+C로 해제)
python deploy/simulate_a10g.py --check  # 점유해야 할 용량만 계산
```

카드 크기 차이(1,536 MiB)만큼 점유하면 **안 된다.** 로컬 데스크톱 잔량(~790 MiB)과
이 스크립트 자신의 CUDA 컨텍스트(~450 MiB)가 이중으로 깎아서 목표보다 1,600 MiB
빡빡해진다. 그러면 "g5에서 안 된다"가 아니라 "내가 만든 조건에서 안 된다"를 재게 된다.
그래서 자기 오버헤드를 실측하고 역산해 **남는 여유를 목표 카드와 일치시킨다**
(실제 23,023 MiB 대 g5의 23,024 MiB, 오차 1 MiB).

### 결과: 세 컨테이너 한 대가 통과한다

반복 부하 8회 × (sam3d + room_layout + omni3d) = 24작업:

| | g5 환산 사용량 |
|---|---|
| 전반부 평균 | 20,966 MiB |
| 후반부 평균 | 20,892 MiB (증가 **-74**) |
| 최악 피크 | 21,181 MiB = 한도의 **92.0%** |

캐시 증가는 **발산하지 않고 수렴한다.** SAM3D 단독 8회 반복에서도 같았다
(전반부 평균 22,464 → 후반부 22,343).

입력 해상도별(실사 침실 사진을 리샘플):

| 해상도 | MP | sam3d 소요 | g5 환산 사용량 |
|---|---|---|---|
| 64×64 | 0.0 | 32.2s | 22,466 MiB |
| 1024×683 | 0.7 | 41.3s | 22,441 |
| 1600×1066 | 1.7 | 32.3s | 21,850 |
| 2560×1707 | 4.4 | 29.2s | 22,451 |
| 4032×2688 | 10.8 | 50.3s | 22,459 |

**해상도는 VRAM에 영향이 없다** — 모델이 내부에서 리사이즈한다. 대신 시간이 늘어난다
(10.8MP에서 50초). VRAM 변동은 해상도가 아니라 SAM3D 시드 탐색 결과(메시 크기)에서
온다. 39개 작업 세트 전부 성공, 최악 97.6%.

### 한 대에 올릴 때 반드시 해야 하는 설정

**PyTorch 캐시는 프로세스 간에 공유되지 않는다.** sam3d 워커가 먼저 캐시를 22GB로
불려버리면 뒤늦게 뜨는 scene 모델은 들어갈 자리가 없어 죽는다. 위 테스트가 통과한 것은
scene 컨테이너가 먼저 로드를 마친 순서 덕이었을 뿐, 보장된 것이 아니다.

지금 compose는 `worker-scene`이 `ulayout`/`omni3d`에 `service_started`로만 의존한다.
이건 **컨테이너 프로세스가 떴다는 뜻일 뿐 모델 로드가 끝났다는 보장이 아니다.**
그리고 `worker-sam3d`는 두 서버에 대한 의존성이 아예 없다.

한 대 구성으로 갈 때 필요한 것:

1. `ulayout`(:8002)과 `omni3d`(:8003)에 헬스체크를 건다. 두 서버 모두 `/health`가
   있지만 **모델이 안 올라가도 `status: ok`를 반환**하므로, `status`가 아니라
   `ulayout_loaded` / `omni3d_loaded` 플래그가 `true`인지 봐야 한다.
2. `worker-sam3d`가 두 서버에 `condition: service_healthy`로 의존하게 한다. 그래야
   scene 모델이 자기 몫(약 1.9GB)을 먼저 확보한 뒤에 sam3d 할당자가 확장한다.

### 나중에 쪼개는 건 코드 변경이 아니다

큐가 이미 환경변수로 분리돼 있다.

- API: `Q_SAM3D` → `emr-sam3d`, `Q_SCENE` → `emr-scene` (`deploy/api/main.py:36`)
- 워커: `QUEUE_NAME` 하나로 어느 큐를 볼지 정한다 (`deploy/worker/worker.py:28`)
- worker-scene은 모델 서버를 `ULAYOUT_URL` / `OMNI3D_URL`로 호출한다

따라서 "한 대"에서 "sam3d와 scene 분리"로 가는 것은 **배치 변경**이지 코드 변경이
아니다. 한 대로 시작해도 갇히지 않는다.

## 비용 모델

`deploy/cost_model.py`가 구성별 월 예상 비용을 계산한다.

```bash
python deploy/cost_model.py
```

온디맨드 단가는 AWS 공식 가격표(`pricing.us-east-1.amazonaws.com`, ap-northeast-2)에서
받은 실값이다. 스팟은 **2026-09-30 `describe-spot-price-history` 실측값**으로
교체했다(서울 3개 AZ 중 최저가 AZ 기준 — `price-capacity-optimized`가 싼 풀을
먼저 고르므로).

| 인스턴스 | GPU | VRAM(실제) | 서울 온디맨드 | 서울 스팟(실측) | 할인율 |
|---|---|---|---|---|---|
| g4dn.xlarge | T4 | 15,360 MiB | $0.647 | $0.320 | 50% |
| **g6.xlarge** | **L4** | ~23,034 MiB | **$0.990** | **$0.454** | **54%** |
| g5.xlarge | A10G | 23,028 MiB | $1.237 | $0.567 | 54% |
| g6e.xlarge | L40S | ~46,068 MiB | $2.288 | $1.240 | 46% |

실측 전에는 g5의 할인율(45.9%)을 g6/g6e에 외삽한 추정치를 썼다. 어디서 맞고
어디서 틀렸는지가 남길 만하다:

| 인스턴스 | 추정 | 실측 | 오차 |
|---|---|---|---|
| g6.xlarge | $0.4543 | $0.4535 | **−0.2%** |
| g6e.xlarge | $1.0502 | $1.2399 | +18.1% |
| g4dn.xlarge | $0.2740 | $0.3204 | +16.9% |

**우리가 고른 ①(g6.xlarge)만 정확했고, 비교 대상인 ②(g4dn 분리)·③(g6e)은 둘 다
실제로 더 비쌌다.** 할인율 외삽은 같은 세대·같은 급(L4↔A10G)에서는 통했지만
L40S와 T4에서는 깨졌다 — 인스턴스 급이 다르면 스팟 수요·공급이 달라서다.
결론적으로 ① 선택은 약화되지 않고 강화됐다.

**g6(L4)이 g5(A10G)와 같은 24GB급인데 20% 싸다.** L4는 메모리 대역폭이 A10G의 절반
(300 대 600 GB/s)이라 추론이 느릴 수 있지만, 스케일투제로에서는 유휴·부팅이 비용을
지배해서 **2~3배 느려도 g6이 유리하다**.

### 비용을 지배하는 건 구성이 아니라 유휴 타임아웃과 부팅 시간

20세션/일, 스팟 우선 기준:

| IDLE_EXIT_SEC | 월 비용 | 유휴 비중 |
|---|---|---|
| 60초 | $59 | 17% |
| 120초 | $65 | 28% |
| **900초 (워커 기본값)** | **$148** | **75%** |
| 1800초 | $243 | 86% |

구성을 바꿔서 아끼는 금액보다 이 숫자 하나를 바꿔 아끼는 금액이 더 크다.

**단, 유휴를 줄이면 이번엔 부팅이 지배한다** (IDLE 120초, g6.xlarge):

| 부팅 | 월 비용 | 부팅 비중 |
|---|---|---|
| 60초 | $45 | 20% |
| 300초 | $66 | 55% |
| 600초 | $92 | 71% |

### 부팅이 길어지는 이유 — 컨테이너가 자기완결적이 아니다

GPU 컨테이너는 conda 환경과 소스를 **호스트에서 bind-mount로 빌려 쓴다.** 이미지가
577MB밖에 안 되는 이유이고, AWS에는 그 호스트가 없으므로 AMI에 다 구워야 한다.

| 항목 | 크기 |
|---|---|
| conda 환경 3개 (sam3d 19G + uLayout 7.0G + omni3d 13G) | 39 GB |
| HuggingFace 캐시 — 실질적인 **모델 가중치 저장소** | 28 GB |
| torch 캐시 (ZoeDepth/DINOv2/PerspectiveFields/resnet50) | 3.4 GB |
| 소스 5개 + 체크포인트 3개 (`.git` 제외) | 3.3 GB |
| 저장소(web 빌드 산출물 제외) | 0.3 GB |
| 도커 이미지 | 3.0 GB |
| OS + NVIDIA 드라이버 (Base DLAMI) | 약 8 GB |
| **AMI 합계** | **약 85 GB** |

이 표를 두 번 틀렸고, 두 번 다 **측정 방법** 때문이었다. 기록해 둔다.

> **1차: 47GB.** HuggingFace 캐시 28GB가 빠져 있었다. 가중치가 conda 환경 안이
> 아니라 `~/.cache/huggingface` 에 있어서 "환경 + 소스"만 세면 제일 큰 덩어리를
> 통째로 놓친다.
>
> **2차: 80GB.** `du -sh envs/sam3d envs/uLayout envs/omni3d` 가 32GB라고 했다.
> `du` 는 **한 번의 호출 안에서 하드링크를 한 번만 센다.** uLayout과 omni3d는
> 둘 다 Python 3.10이라 7GB를 공유하고 있었고, 그게 한 번만 세어졌다.
> `du --count-links` 로 다시 재면 19 + 7.0 + 13 = **39GB**다. conda-pack 결과물은
> 하드링크가 없으니 EC2에서 차지하는 건 39GB 쪽이다. omni3d의 tar가 `du` 가
> 말한 5.9G가 아니라 13G로 나와서 알았다.

**굽는 도중 피크도 약 85GB다.** 예전 계획(EC2에서 `conda create` 로 환경을 다시
설치)이었다면 `~/miniconda3/pkgs` 에 패키지가 35GB까지 쌓여 피크가 115GB였다.
지금은 `pack-envs.sh` 가 개발 PC에서 포장한 것을 `bootstrap-ami.sh` 가 S3에서
**파이프로 바로 풀기** 때문에 패키지 캐시도, 중간 tarball도 생기지 않는다.

그래도 루트 볼륨은 150GB로 잡는다(`EMR_VOLUME_GB`). 스냅샷 요금은 **쓴 블록만**
세므로 안 쓴 65GB는 돈을 안 낸다 — 넉넉히 잡는 쪽이 싸다. 빌더 인스턴스도 같은
150GB로 띄워야 한다: **AMI 스냅샷 크기가 빌더의 볼륨 크기로 굳고, 그게
`EMR_VOLUME_GB` 의 하한이 된다**(더 크게 띄우면 `20-launch-template.sh` 가
"스냅샷보다 작은 볼륨" 이라고 거부한다).

스냅샷에서 복원한 EBS 볼륨은 블록을 **처음 읽을 때** S3에서 지연 로딩된다. 이 중 실제로
읽는 10GB 남짓을 실효 50~100MB/s로 당겨오면 첫 부팅에만 2분 이상이 더 붙는다. EBS Fast
Snapshot Restore가 이 문제를 없애주지만 스냅샷·AZ당 시간 $0.75(월 $540)라 스케일투제로
취지에 정면으로 어긋난다 — 쓰지 않는다. 대신 루트 볼륨 처리량을 gp3 기본 125에서
250 MB/s로 올려뒀다(`EMR_VOLUME_THROUGHPUT`). 125 초과분은 MB/s당 월 $0.04이고
인스턴스가 떠 있는 시간만큼만 비례 과금된다.

### AMI를 어떻게 만드나 — 설치가 아니라 이사다

처음에는 EC2에서 설치를 재현할 계획이었다(집 회선으로 51GB를 올리는 것보다 AWS
안에서 받는 쪽이 빠르고 공짜다). 실제 상태를 들여다보고 접었다. **재현할 명세가
없다.**

| 재현을 막은 것 | 실태 |
|---|---|
| conda 환경 3개 | `environment.yml` 도 requirements 고정본도 없다. 손으로 패치해 맞춘 결과물이고, `conda create` 는 그날 인덱스에 따라 다른 버전을 고른다. |
| 소스 3개 | 커밋 안 된 수정이 있다 — sam-3d-objects 7파일 + `depth_pro.py` 신규, uLayout `room_rectify.py`, omni3d는 `server.py`·`configs`·`tools` 가 전부 untracked. `git clone` 은 이걸 하나도 안 가져오면서 **성공한다.** |
| HF 캐시 | 새로 받아서는 재현되지 않는 상태다(아래). |
| `facebook/sam-3d-objects` | 게이트 저장소. 받으려면 인스턴스에 HF 토큰을 올려야 한다. |
| `runwayml/stable-diffusion-inpainting` | 허브에서 내려갔을 수 있다(4GB). |

HF 캐시 건이 결정적이었다. 파이프라인이 읽는 경로는
`sam-3d-objects/checkpoints/hf/checkpoints/*.ckpt` 인데, 이게 **blob 파일명(sha256)으로
걸린 심링크 7개**다. 그런데 그 저장소의 `snapshots/` 디렉터리는 비어 있다 — 가중치
12.3GB는 `blobs/` 에만 있다. 새로 받으면 `snapshots/` 는 제대로 채워지는 대신 blob
이름이 달라질 수 있고, 그러면 심링크가 끊긴다. **AMI를 다 구운 뒤 첫 추론에서야
드러나는 종류의 고장이다.**

그래서 전부 올린다(≈51GB). S3 수신은 공짜고 한 번 치르는 비용이다. 대신 외부 의존이
0이 된다 — 게이트도, 토큰도, 삭제된 저장소도, blob 해시 의존도 없다.

운이 좋았던 점 하나: 그 심링크 7개는 `../../../../.cache/...` 즉 **상대 경로**다.
루트가 `$HOME` 에서 `/opt/emr` 로 바뀌어도 같은 상대 위치를 가리키므로 손댈 필요가
없었다.

| 순서 | 어디서 | 무엇 |
|---|---|---|
| 1 | 개발 PC | `./pack-envs.sh` — conda-pack(`--dest-prefix /opt/emr/...`)으로 환경을 포장하고 캐시·소스·체크포인트와 함께 S3에 올린다 |
| 2 | 개발 PC | `./launch-builder.sh` — Base DLAMI + g6.xlarge **스팟** 1대, 루트 **150GB**, emr 인스턴스 역할 |
| 3 | 빌더 안 | `tmux` 안에서 `sudo bash bootstrap-ami.sh` — S3에서 **파이프로 바로** 풀어 `/opt/emr` 을 만든다 |
| 4 | 빌더 안 | `./bake-ami.sh` → `./verify-ami.sh` |
| 5 | 개발 PC | `aws ec2 create-image` → `./launch-builder.sh terminate` |

**왜 스팟인가.** 승인된 쿼터가 "G/VT **스팟** 8 vCPU"다. 온디맨드 G 쿼터는 완전히
별개 항목이고 0일 수 있어서, 확실히 뜨는 쪽을 쓴다. g6.xlarge 는 4 vCPU라 8 안에
들어가고, 굽는 동안 ASG는 0대라 쿼터가 통째로 비어 있다. 값도 시간당 $0.99 대신
$0.3 안팎이다.

스팟이라 중단될 수 있고, 중단되면 루트 볼륨도 같이 사라진다
(`DeleteOnTermination=true`). 받아둔 85GB가 날아가니 처음부터 다시다. 볼륨을
남기는 선택지도 있지만 **그쪽이 더 위험하다** — 인스턴스가 사라진 뒤에도 150GB가
조용히 과금되는 게 이 프로젝트에서 제일 경계하는 누수 형태다.

**5번을 강조하는 이유** — 빌더는 ASG 밖의 맨 인스턴스다. 리퍼도 가디언도 그 안에서
돌지 않으므로(가디언은 `bake-ami.sh` 가 설치하지만 ASG에 속한 인스턴스만 회수한다)
**아무도 회수해 주지 않는다.** 스팟이라 언젠가는 중단되겠지만, 그게 언제일지로
요금을 통제할 수는 없다.

### 중단된 다운로드가 조용히 통과하던 문제

`bootstrap-ami.sh` 의 재개 판단이 원래 **디렉터리 존재**였다
(`[ -d $EMR_ROOT/sam-3d-objects ]` 같은 식). 그런데 tar는 *첫 파일*을 풀 때 이미
그 디렉터리를 만든다. 그래서 스트림이 중간에 끊긴 뒤 다시 돌리면 반쯤 풀린 조각을
완성된 것으로 보고 건너뛰고, 그렇게 구운 AMI는 멀쩡히 부팅한 뒤 첫 추론에서 죽는다.

온디맨드였다면 드물었겠지만 스팟 빌더에서는 흔한 경로다 — 1~2시간짜리 다운로드
중에 노트북이 절전되거나 wifi가 한 번만 끊겨도 이 상태가 된다. 지금은 tar가
종료코드 0으로 끝난 뒤에만 `$EMR_ROOT/.bootstrap-done/<조각>` 에 표식을 남기고,
건너뛸지는 **표식으로만** 판단한다. 체크포인트 3개도 `.part` 로 받고 끝난 뒤에
`mv` 한다(받다 만 파일도 `[ -s ]` 는 통과하기 때문).

그래서 bootstrap은 **반드시 `tmux` 안에서** 돌린다. 재개가 되더라도, 끊긴 조각을
처음부터 다시 받는 건 여전히 수십 분이다.

`pack-envs.sh` 의 스테이징은 기본값이 `/mnt/d` 다. WSL2에서 `df /` 는 여유 779GB라고
하지만 그 ext4는 C드라이브 위의 vhdx 파일이고 C에는 35GB밖에 없다. WSL 안에 28GB짜리
tar를 만들면 vhdx가 커지며 C를 채우고, 그때부터 CUDA가 엉뚱한 오류를 낸다. 전에
한 번 당했다 — `dmesg` 의 `dxg` 줄에 `-75` 가 찍힌다.

## 4단계: 오토스케일링 + 스케일투제로

결론부터: **용량 전이 네 가지를 서로 다른 장치가 맡는다.** 하나로 통일하려다
실패하는 게 스케일투제로에서 가장 흔한 사고다. 왜 나눠야 했는지가 이 단계의 내용
전부다.

| 전이 | 담당 | 지연 | 파일 |
|---|---|---|---|
| 0대 → 1대 | API 서버가 접수 순간 직접 요청 | 0초 | `api/capacity.py` |
| 1대 → N대 | 백로그 알람 + 단계 조정 정책 | 1~2분 | `aws/40-scaling.sh` |
| 스팟 실패 → 온디맨드 1대 | 별도 ASG + ASG 지표 알람 | 5분 | `aws/30-asg.sh`, `aws/40-scaling.sh` |
| N대 → 0대 | 인스턴스 안의 리퍼 | 유휴 120초 | `worker/idle_reaper.py` |

### 왜 "큐 길이로 오토스케일링"만으로는 안 되는가

처음 설계는 교과서대로였다. SQS 큐 길이를 보는 타깃 추적 정책 하나. 그런데
검증하면서 **네 가지가 차례로 깨졌다.**

**(1) 타깃 추적은 0대에서 동작하지 않는다.**
큐 길이는 타깃 추적에 쓸 수 없는 지표다(AWS 문서가 명시적으로 금지한다 — 메시지
수는 인스턴스 대수에 비례해 줄지 않기 때문). 권장 방식은 "인스턴스당 백로그"
= 큐 길이 ÷ 가동 대수인데, **0대면 분모가 0이라 값이 정의되지 않는다.** 지표가
없으면 알람은 `INSUFFICIENT_DATA`가 되고 그룹은 0대에 영원히 머문다.

**(2) 잠든 큐는 최대 15분 늦게 깨어난다.**
> "큐가 6시간 넘게 비활성이면 Amazon SQS는 CloudWatch로 지표 전송을 멈춘다."
> "비활성 상태에서 활성화될 때 CloudWatch 지표에 **최대 15분의 지연**이 발생한다."

하루 수십 세션이면 밤새 큐가 잔다. 아침 첫 요청은 지표 지연 15분 + 알람 1분 +
부팅 3분 = **19분**을 기다리게 된다. 비용을 아끼려다 서비스를 못 쓰게 만드는 셈이다.

그래서 0→1은 CloudWatch를 아예 안 쓴다. 접수 순간을 정확히 아는 쪽 —
상시 가동인 API 서버 — 이 `SetDesiredCapacity(1)`을 직접 부른다. 지연 0초다.
알람은 버리지 않고 **1→N 증설 전용**으로 역할만 좁혔다. 그때는 큐가 이미
활성이라 지표가 1분마다 정상적으로 온다.

**(3) 가장 바쁜 순간이 CloudWatch에는 가장 한가해 보인다.**
SAM3D가 30초짜리 작업을 처리하는 동안 그 메시지는 "처리 중(NotVisible)"이라
큐에서 안 보인다. `ApproximateNumberOfMessagesVisible`만 보고 스케일인을 걸면
**작업 중인 인스턴스를 골라 죽인다.** 작업이 사라지진 않지만(가시성 타임아웃이
지나면 큐로 돌아온다) GPU 30초를 버리고 처음부터 다시 한다.

축소 판단은 "지금 일하는 중인지"를 유일하게 아는 쪽, 즉 인스턴스 자신이 한다.
ASG 쪽 스케일인 정책은 아예 만들지 않았다.

**(4) ASG에는 스팟 → 온디맨드 자동 폴백이 없다.**
혼합 인스턴스 정책의 `OnDemandPercentageAboveBaseCapacity`는 "정상일 때의 배합
비율"일 뿐, 스팟 용량이 없을 때 대신 온디맨드를 사는 동작이 **아니다.** 스팟이
안 잡히면 ASG는 조용히 재시도만 반복하고 큐는 계속 쌓인다. 로그도 한동안 안 남는다.

그래서 직접 만들었다. 그룹을 둘로 둔다.

```
emr-gpu-spot      전량 스팟, min=0, max=3.  평소 여기만 쓴다.
emr-gpu-ondemand  전량 온디맨드, min=0, max=1.  평소 0대.
```

폴백 알람은 **SQS를 아예 안 본다.** ASG 자기 지표만 본다:

```
IF(GroupDesiredCapacity > 0 AND GroupInServiceInstances < GroupDesiredCapacity, 1, 0)
```

"원한 만큼 못 띄우고 있다"가 5분 연속이면 스팟 용량이 없는 것이다. ASG 지표는
인스턴스가 0대여도 1분마다 계속 나오므로 (2)의 15분 지연 문제에서도 자유롭다.
Lambda도 EventBridge도 필요 없다.

회복도 저절로 된다. 스팟이 다시 잡히면 온디맨드 쪽 워커는 할 일이 없어 유휴가
되고, 그 인스턴스의 리퍼가 스스로 내린다. 축소 경로가 하나뿐이라 "둘이 동시에
줄이다 작업 중인 인스턴스를 죽이는" 경우가 생기지 않는다.

### 리퍼: 왜 워커가 직접 안 죽고 별도 프로세스인가

①(세 컨테이너 한 대) 구성에서는 한 인스턴스에 워커가 둘(sam3d, scene)이다.
sam3d 워커가 2분 놀았다고 인스턴스를 내리면, 바로 옆에서 scene 워커가 돌리던
작업이 같이 죽는다.

그래서 워커는 자기 상태를 파일로 알리기만 한다.

```
/state/emr-sam3d.state   {"busy": false, "updated_at": 1759..., "pid": 1}
/state/emr-scene.state   {"busy": true,  "updated_at": 1759..., "pid": 1}
```

판단은 인스턴스에 하나뿐인 리퍼가 모아서 한다. 종료 조건은 넷을 전부 만족할 때만이다.

1. 기대하는 워커가 **전부** 상태 파일을 냈다 (하나라도 없으면 기동 중 → 보류)
2. 전부 `busy: false`
3. 그 상태가 `IDLE_EXIT_SEC`(120초) 동안 유지됐다
4. 종료 직전 큐를 한 번 더 확인해 대기 + 처리중이 0이다

하나라도 어긋나면 카운터를 리셋한다. **상태 파일이 90초 넘게 갱신되지 않으면
"바쁨"으로 친다.** 멎은 워커를 유휴로 오해해서 멀쩡한 작업과 함께 인스턴스를
내리는 쪽이 훨씬 나쁘기 때문이다. 요금이 조금 더 나가는 건 되돌릴 수 있지만
날아간 작업은 못 되돌린다.

종료는 이 한 줄이다.

```python
asg.terminate_instance_in_auto_scaling_group(
    InstanceId=iid, ShouldDecrementDesiredCapacity=True)
```

`ShouldDecrementDesiredCapacity=True`가 4단계 전체의 성패다. `False`로 두면
ASG는 desired를 지키려고 **즉시 새 인스턴스를 띄운다.** 스케일투제로가 무한
재기동 루프가 되고 요금은 계속 나간다.

### 조용히 터지는 설정 두 가지

**IMDS 홉 한도.** 시작 템플릿의 `HttpPutResponseHopLimit`은 기본이 1이다.
도커 브리지 네트워크가 그 한 홉을 써버려서 **컨테이너 안에서는 인스턴스
메타데이터가 타임아웃난다.** 리퍼가 자기 `instance-id`도, 스팟 회수 통지도 못
읽고 조용히 아무 일도 안 한다 — 에러 로그조차 안 남는다. 2로 올려야 한다.
(`HttpTokens=required`로 IMDSv1은 막는다. SSRF 한 방에 자격증명이 새는 경로다.)

**ASG 그룹 지표 수집.** `enable-metrics-collection`을 안 하면
`GroupInServiceInstances` / `GroupDesiredCapacity`가 아예 안 나온다. 폴백 알람이
그 둘을 보므로, 빠뜨리면 알람이 `INSUFFICIENT_DATA`로 굳고 "스팟이 안 뜨는데
아무 일도 안 일어나는" 상태가 된다.

**알람 수식의 `FILL(...,0)`.** SQS는 해당 상태의 메시지가 하나도 없으면 그 지표를
**아예 안 보낸다.** 결측이 하나라도 섞이면 수식 전체가 결측이 되어 알람이
`INSUFFICIENT_DATA`로 빠진다. `FILL(s1,0)+FILL(s2,0)+...` 로 메워야
"sam3d에만 일이 있는" 정상 상황을 제대로 읽는다.

### 스팟 회수 대응

회수는 막을 수 없으니 싸게 만든다. 안전망은 3중이다.

1. **메시지를 성공했을 때만 지운다.** 인스턴스가 중간에 죽어도 가시성 타임아웃이
   지나면 메시지가 큐로 돌아온다. 3회 실패하면 DLQ. 작업은 안 사라진다.
2. **리퍼가 IMDS로 회수 통지(2분 전)를 폴링하다 `DRAIN` 파일을 만든다.** 워커는
   그걸 보고 새 작업을 더 안 받는다. 도커가 SIGTERM을 보내는 건 실제 종료 직전이라,
   그때까지 30초짜리 작업을 계속 집어가면 통째로 버려진다.
3. **`stop_grace_period: 90s`.** 도커 기본값 10초로는 진행 중인 SAM3D 작업을
   못 끝내고 SIGKILL 당한다.

여기에 ASG의 `--capacity-rebalance`를 켜서, 회수 예고를 받으면 회수당하기 전에
대체 인스턴스를 미리 띄운다.

스팟 풀은 최대한 넓혔다. 전부 24GB급 단일 GPU다.

```
g6.xlarge  g6.2xlarge  g5.xlarge  g5.2xlarge  g6e.xlarge
```

2xlarge는 GPU가 같고 CPU/RAM만 넉넉한 것이라 그대로 쓸 수 있다. 후보를 넓힐수록
"용량 없음"이 줄고, 그만큼 온디맨드 폴백이 덜 깨어난다. 할당 전략은
`price-capacity-optimized`다 — `lowest-price`는 가장 싼 풀만 골라서 그 풀이
고갈되면 바로 회수당하고, `capacity-optimized`는 회수는 적지만 비쌀 수 있다.

### 요금이 새는 유일한 경로와 그걸 막는 장치

스케일투제로에서 돈이 새는 방식은 하나뿐이다. **일을 안 하는데 살아있는 인스턴스.**
4단계까지 만든 구조를 다시 보면 그 경로가 실제로 열려 있었다.

```
docker compose up -d 실패
  → 리퍼(idle_reaper) 컨테이너가 안 뜸
  → 아무도 인스턴스를 회수하지 않음
  → ASG 헬스체크는 --health-check-type EC2 (켜져 있는지만 본다) → 정상으로 판단
  → g6.xlarge 가 일 없이 시간당 $0.45, 한 달 $327
```

핵심은 **스케일투제로 장치가 자기가 보호해야 할 대상(컴포즈 스택)에 의존**하고
있었다는 점이다. 리퍼는 컴포즈 서비스라서, 컴포즈가 실패하면 같이 없다.

그래서 고리를 끊는 장치를 컴포즈 **바깥**에 뒀다.

| 계층 | 위치 | 하는 일 | 못 하는 일 |
|---|---|---|---|
| 리퍼 | 컴포즈 컨테이너 | 유휴 120초 + 큐 빔 → 스스로 회수 | 컴포즈가 안 뜨면 존재하지 않음 |
| 가디언 | 호스트 systemd 타이머(1분) | 리퍼가 5분간 없으면 인스턴스 회수 | 작업 중인지는 판단 안 함 |
| userdata 트랩 | 부팅 스크립트 | 기동 실패하면 즉시 회수 | 부팅 이후는 모름 |

가디언은 일부러 멍청하게 뒀다. 전제는 한 줄이다 — **"리퍼가 없으면 이 인스턴스는
스스로 물러날 방법이 없다 → 물러나게 한다."** 작업 중인지 판단하는 건 리퍼의
일이고, 가디언이 그걸 또 구현하면 버그 날 곳이 두 배가 된다. 처리 중이던 작업은
워커가 하트비트로 가시성 타임아웃을 늘리고 있으므로, 죽으면 큐로 돌아가 재시도된다.

세 계층이 전부 `aws/self-retire.sh` 하나를 호출한다. 종료 로직을 나눠 구현하지
않는 이유는 `--should-decrement-desired-capacity` 때문이다. 이걸 빠뜨리면 증상이
"스케일투제로가 안 된다"가 아니라 **"인스턴스가 무한히 재생성된다"**로 나타나서
원인을 찾기 어렵다. 같은 이유로 `shutdown`은 대안이 아니다 — ASG가 unhealthy로
보고 desired를 유지한 채 교체 인스턴스를 띄운다. 요금이 그대로 이어진다.

`self-retire.sh`는 aws CLI가 있어야 동작한다. 없으면 요금을 끊을 방법이 아예
없는데, 그 사실은 고지서로만 알게 된다. 그래서 **굽기 전에** `verify-ami.sh`가
막는다.

### AMI 굽기

부팅 때 하는 일이 적을수록 콜드스타트가 짧고, 콜드스타트가 짧을수록 유휴
타임아웃을 줄일 수 있다(비용을 지배하는 건 이 둘이다). 그래서 모델 가중치는
물론 **도커 이미지까지 AMI에 미리 굽는다.**

`docker compose up -d`는 이미지가 없으면 그 자리에서 빌드한다. 실패하는 게
아니라 **수십 분 걸리는 빌드를 조용히 시작한다.** 반대로 Dockerfile을 고쳐도
이미지가 이미 있으면 다시 빌드하지 않는다 — 옛 이미지가 조용히 쓰인다. 어느
쪽이든 "무슨 코드가 돌고 있는지 모르는" 상태라, git SHA를 이미지 태그로 박고
부팅 때 그 태그가 실제로 있는지 확인한다. 없으면 빌드하지 않고 크게 실패한다.

```bash
# AMI 원본 인스턴스 안에서
cd /opt/emr/Empty_My_Room/deploy/aws
./bake-ami.sh      # 이미지 빌드(git SHA 태그) + 가디언 설치 + 스냅샷 위생
./verify-ami.sh    # 통과해야 스냅샷을 찍는다
aws ec2 create-image --instance-id i-xxxx --name emr-gpu-<sha> --no-reboot
```

`verify-ami.sh`가 보는 것: aws CLI, `/opt/emr/bin/self-retire.sh`,
`emr-guardian.timer` enabled, docker 자동 시작, nvidia 런타임, 세 이미지 태그
존재, `emr/worker`와 `emr/gpu`가 서로 다른 이미지인지(태그 덮어쓰기 사고),
가중치 디렉터리, 마운트 경로와 이미지에 구워진 `EMR_ROOT` 가 일치하는지,
HF 캐시 소유자가 uid 1000인지, conda 패키지 캐시가 정리됐는지,
`.env`와 `~/.aws`가 남아있지 않은지.

마지막 항목을 확인할 때 `sudo -n`을 쓴다. sudo가 비밀번호를 물어보며 실패하면
`test -e`도 실패하는데, 그걸 "파일 없음"으로 읽으면 **확인하지 못한 것을
통과로 세게 된다.** 검증 스크립트에서 제일 위험한 버그 유형이다.

#### 부팅 때 하는 일 (`userdata.sh`)

| 단계 | 하는 일 | 실패하면 |
|---|---|---|
| 저장소 확인 | `$EMR_REPO_DIR` 존재 확인 | 회수 |
| 모델 루트 확인 | `$EMR_ROOT` 아래 마운트 대상 7개 | 회수 |
| 이미지 태그 확인 | 세 이미지가 AMI에 있는지 | 회수(빌드 안 함) |
| 환경변수 주입 | `deploy/.env` 생성(umask 077) | 회수 |
| 스냅샷 워밍 | 가중치 파일 미리 읽기 | 계속(느려질 뿐) |
| 컴포즈 기동 | `up -d --no-build` | 회수 |
| 리퍼 확인 | 30초 안에 떴는지 | 회수 |

트랩을 `ERR`이 아니라 `EXIT`에 건다. `ERR` 트랩은 `exit 1`로 일부러 죽는
자리에서는 돌지 않는데, 정작 그 자리들이 제일 회수해야 하는 경우다.

**스냅샷 워밍을 장치명이 아니라 파일 경로로 하는 이유.** 원래는
`fio --filename=/dev/nvme1n1 --runtime=180 ... || true` 였다. 문제가 셋이다.

- 장치 번호는 볼륨 구성에 따라 바뀐다. 가중치가 루트 볼륨(`nvme0n1`)에 있으면
  엉뚱한 장치를 읽는데, `|| true` 때문에 **조용히** 넘어간다.
- `fio`의 `--runtime`은 상한이다("둘 중 먼저 오는 쪽"). 125MB/s gp3에서 180초면
  80GB 중 22GB만 데운다. 나머지는 첫 요청 때 지연 로딩된다.
- 볼륨 전체를 읽을 필요도 없다. 필요한 건 가중치뿐이다.

지금은 `EMR_WARM_DIRS`의 8MB 이상 파일만 `EMR_WARM_TIMEOUT`(600초) 안에서 읽는다.
장치와 무관하고, 없는 디렉터리는 건너뛰되 로그에 남긴다. 순서가 곧 우선순위라
HF 캐시 → conda 환경 → 소스 순으로 둔다(상한에 걸리면 뒤쪽은 첫 요청 때 읽힌다).

> `EMR_WARM_DIRS` 는 한동안 `$EMR_REPO_DIR/sam-3d-objects` 처럼 **존재하지 않는**
> 경로를 가리키고 있었다(실제 위치는 `$EMR_ROOT/` 아래다). `warm()` 은 없는
> 디렉터리를 건너뛰므로 에러도 안 나고, 워밍이 0바이트인 걸 알 방법이 없었다.
> 지금은 `verify-ami.sh` 가 `EMR_WARM_DIRS` 의 모든 경로를 실제로 확인한다.

#### 왜 경로를 `/opt/emr` 로 통일했나

GPU 컨테이너는 conda 환경과 소스를 **호스트와 똑같은 절대경로**로 마운트한다.
conda prefix와 editable 설치가 경로를 파일 안에 박아두기 때문에, 다른 위치에
붙이면 `import` 부터 깨진다(`deploy/gpu/Dockerfile` 머리말 참고).

문제는 그 경로가 개발 PC의 홈(`/home/<개인계정>`)으로 **박혀** 있었다는 것이다.

- `gpu/Dockerfile` 의 `CMD` 가 그 경로의 python을 직접 가리켰다
- `docker-compose.gpu.yml` 의 마운트·`command`·`healthcheck` 가 전부 `${HOME}` 기준
- 게다가 userdata는 root로 도니까 거기서는 `${HOME}` 이 `/root` 가 된다

EC2 기본 사용자는 `ubuntu` 라 그 경로가 아예 없다. 그런데 **도커는 마운트 원본이
없어도 에러를 내지 않는다** — 빈 디렉터리를 root 소유로 만들어 준다. 컨테이너는
정상적으로 뜨고, 리퍼도 뜨고, ASG도 정상으로 보다가, 첫 요청에서
`ModuleNotFoundError` 로 죽는다. 그동안 요금은 계속 나간다.

그래서 루트를 `EMR_ROOT` 로 뺐다.

| | 값 | 어디서 |
|---|---|---|
| 로컬 | `${HOME}` (= 전과 동일) | `EMR_ROOT` 미설정 → compose 기본값 |
| AWS | `/opt/emr` | `config.sh` → `deploy/.env` → compose |
| 이미지 | 빌드 인자 `EMR_ROOT` 로 구움 | `docker-compose.gpu.yml` 의 `args` |

덤으로 Dockerfile에서 개인 계정명이 사라졌다. 이 PC를 같이 쓰는 다른 두 사람도
이제 이미지를 빌드해서 쓸 수 있다(전에는 빌드는 되는데 실행이 안 됐다).

경로가 어긋났을 때 **첫 요청까지 가지 않고** 깨지도록 세 군데서 막는다.

| 언제 | 무엇을 | 어디 |
|---|---|---|
| 굽기 전 | 저장소 위치 = `EMR_REPO_DIR`, 마운트 원본 7개, HF 캐시 소유자 uid 1000 | `bake-ami.sh` |
| 스냅샷 전 | 위 전부 + **이미지에 구워진 `EMR_ROOT` 가 설정과 같은지** | `verify-ami.sh` |
| 부팅 때 | 마운트 원본 7개 | `userdata.sh` (실패 시 회수) |

컨테이너가 uid 1000으로 도는 것도 같이 본다. HuggingFace 캐시는 락 파일을 쓰므로
읽기만으로는 부족한데, `/opt/emr` 을 root로 깔아두기 쉽다. 그러면 첫 추론에서
`PermissionError` 다 — `sudo chown -R 1000:1000 /opt/emr`.

### 실행 순서

```bash
cd deploy/aws
# 0) 이름/용량/후보 타입을 여기서 한 번에 정한다
vi config.sh

./05-preflight.sh              # 자격증명 + GPU 쿼터 — 통과해야 다음으로
./10-iam.sh                    # 역할 2개 (GPU 워커 / API 서버)
./15-resources.sh              # 큐 2개(+DLQ) / S3 버킷 / DynamoDB 테이블
./10-iam.sh                    # 버킷 이름이 정해진 뒤 정책 ARN을 다시 맞춘다

# --- 여기서 AMI를 굽는다 (원본 인스턴스 안에서 bake-ami.sh → verify-ami.sh) ---

EMR_AMI_ID=ami-xxxx EMR_SG_ID=sg-xxxx ./20-launch-template.sh
EMR_SUBNETS=subnet-a,subnet-b,subnet-c ./30-asg.sh
./40-scaling.sh                # 정책 + 알람 + 그룹 지표 수집
```

#### 10-iam.sh 를 두 번 돌리는 이유

S3 버킷 이름은 **계정별이 아니라 전 세계에서 유일**해야 한다. 그래서
`config.sh` 가 계정 ID의 해시 앞 8자리를 붙여 `emr-jobs-xxxxxxxx` 를 만든다.
계정 ID를 그대로 붙이지 않은 건, 브라우저가 결과 메쉬를 presigned URL 로 S3에서
직접 받기 때문이다 — 버킷 이름이 곧 사용자에게 보이는 URL 에 들어간다.

IAM 정책은 이 버킷 ARN을 박아서 쓰므로, 버킷을 만들기 전에 돌린 정책은 존재하지
않는 이름을 가리킨다. 리소스를 만든 뒤 한 번 더 돌려서 맞춘다. 두 스크립트 다
여러 번 돌려도 안전하다.

로컬(LocalStack)은 전역 유일성이 필요 없어서 `emr-jobs` 를 그대로 쓴다. 이름이
다른 건 의도된 것이고, 코드는 양쪽 다 환경변수로만 읽는다.

#### 왜 사전점검이 맨 앞인가

**신규 AWS 계정은 G 계열(GPU) 인스턴스 쿼터가 0이다.** 2026-09-30에 이 계정에서
실제로 확인한 값:

```
L-3819A6DF  All G and VT Spot Instance Requests      0.0
L-DB2E81BA  Running On-Demand G and VT instances     0.0
```

쿼터가 0인 채로 10~40번을 다 돌리면 **전부 성공한다.** 역할도, 시작 템플릿도,
ASG도, 알람도 다 만들어진다. 그런데 인스턴스가 한 대도 안 뜬다 — ASG는 용량
부족을 조용히 재시도만 하고, 폴백 알람도 5분마다 온디맨드를 시도했다가 같은
이유로 실패한다. 배포가 "성공"한 것처럼 보이는 게 제일 나쁘다.

쿼터 단위는 **대수가 아니라 vCPU**다. `05-preflight.sh`는 `INSTANCE_TYPES`를
`describe-instance-types`로 조회해 가장 큰 타입 기준 최악의 경우로 계산한다.

#### 부분 승인을 받으면 구성을 쿼터에 맞춰 줄인다

2026-10-01 심사 결과는 이랬다.

```
스팟    24 요청 →  8 승인 (CASE_OPENED, 아직 열려 있음)
온디맨드  8 요청 →  8 승인 (CASE_CLOSED)
```

온디맨드가 통과한 게 더 중요하다. 0이었으면 폴백 ASG가 **정작 필요한 순간에
못 뜨는** 상태였다.

스팟 8 vCPU에 맞추려고 두 가지를 바꿨다.

| | 전 | 후 | 이유 |
|---|---|---|---|
| `MAX_SPOT` | 3 | 2 | 8 ÷ 4 vCPU = 2대 |
| `INSTANCE_TYPES` | 5종 | 3종 (2xlarge 제외) | 2xlarge는 **한 대가 8 vCPU를 다 쓴다** |

2xlarge를 뺀 게 핵심이다. GPU는 xlarge와 같아서 성능 손해는 없는데, vCPU가 8이라
ASG가 그걸 고르는 순간 두 번째 인스턴스가 `VcpuLimitExceeded`로 못 뜬다. 그 실패는
ASG 활동 기록에만 남고 애플리케이션에서는 **"그냥 좀 느리네"로 보인다** — 쿼터
0일 때와 같은 종류의, 성공처럼 보이는 실패다.

대신 스팟 풀이 5종에서 3종으로 좁아져서 회수 확률은 올라간다. 24 vCPU 신청이
아직 열려 있으니, 승인되면 `MAX_SPOT=3` + 2xlarge를 되돌리면 된다.

```
g6.xlarge=4  g5.xlarge=4  g6e.xlarge=4
→ 스팟 4×MAX_SPOT(2) = 8 vCPU / 온디맨드 4×MAX_OD(1) = 4 vCPU
```

여유가 정확히 0이라 사전점검이 그 사실을 따로 찍는다. 지금 구성은 돌아가지만
한 대도 더 못 늘린다는 뜻이다.

스팟과 온디맨드는 **별개 쿼터**라 둘 다 올려야 한다. 스팟만 올리면 평소엔
멀쩡하고, 하필 스팟 물량이 마른 날 폴백이 쿼터 0으로 거부당한다 — 비상구를
만들어놓고 열쇠가 없는 상태다.

증설은 자동 승인이 아니라 **사람 심사(몇 시간~며칠)** 다. 콘솔 Service Quotas에서
리전을 `ap-northeast-2`로 **먼저 바꾸고** 신청할 것. 안 바꾸면 버지니아 쿼터가
올라가서 아무 효과가 없다.

권한은 최소로 좁혀 두었다. 특히 **API 서버에는 종료 권한을 주지 않는다** —
API는 용량을 올리기만 하고, 내리는 건 작업 중인지 아는 리퍼만 한다. 리퍼의
종료 권한도 `autoscaling:ResourceTag/Project = emr` 조건으로 묶어서, 이 역할을
얻은 코드가 계정 안의 아무 ASG나 비울 수 없게 했다.

> `Project=emr` 태그는 장식이 아니다. IAM 조건이 이 태그를 보므로, ASG에 태그가
> 없으면 리퍼가 `AccessDenied`로 인스턴스를 못 내린다 = 스케일투제로가 통째로 안 된다.

### 로컬에서 확인하는 법

리퍼는 `DRY_RUN=1`이면 판단만 로그로 찍고 아무것도 죽이지 않는다. 기본 컴포즈가
그렇게 되어 있다.

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml up -d
docker logs -f deploy-reaper-1
```

작업을 넣으면 `유휴 해제 (... 작업중=['emr-sam3d'])`, 끝나고 120초 지나면
`전원 유휴 진입` → `큐 비었음 — 인스턴스 회수` → `DRY_RUN — 실제로는 종료하지 않는다`
순으로 찍힌다.
