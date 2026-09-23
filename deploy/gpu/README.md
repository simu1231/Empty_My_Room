# GPU 워커 이미지 (3단계)

## 왜 환경을 이미지에 굽지 않는가

실측값이다(`du -sh`):

| 구성요소 | 크기 |
|---|---|
| conda `sam3d` | 19GB |
| conda `uLayout` | 7.0GB |
| conda `omni3d` | 5.9GB |
| `~/.cache/huggingface` (DINOv2 등 실질 가중치) | 28GB |
| `~/.cache/torch` | 3.4GB |
| 체크포인트 (`sam-3d-objects` 1.8G, `uLayout/ckpt` 695M, `omni3d/checkpoints` 364M) | 2.9GB |
| **합계** | **약 66GB** |

이걸 이미지에 넣으면 **스케일투제로의 콜드스타트마다 66GB를 내려받게 된다.**
우리가 없애려던 지연을 우리가 다시 만드는 셈이다.

그래서 이미지에는 **코드와 OS 의존성만** 넣고, 환경과 가중치는 볼륨으로 붙인다.
실제로 빌드해보니 GPU 이미지는 **577MB**다.

| 구성요소 | 크기 | 로컬 | AWS |
|---|---|---|---|
| 이미지 (코드 + OS 라이브러리) | 577MB | 빌드 | ECR |
| conda 환경 | 32GB | 호스트 바인드 마운트 | EBS 스냅샷 |
| 모델 가중치 + 캐시 | 34GB | 호스트 바인드 마운트 | 같은 EBS |

EBS 스냅샷은 인스턴스에 붙는 즉시 쓸 수 있고(블록을 필요할 때 당겨온다),
스냅샷은 여러 인스턴스가 동시에 복제해 붙일 수 있다. 오토스케일링으로
인스턴스가 늘어날 때 각자 66GB를 ECR에서 당기는 것보다 훨씬 빠르고 싸다.

## 왜 환경을 소스 빌드로 재현하지 않는가

`sam3d`에는 kaolin, nvdiffrast, pytorch3d, spconv, xformers가 torch 2.5.1+cu121에
맞춰 컴파일되어 있고, `omni3d`에는 detectron2와 pytorch3d가 editable로 붙어 있다.
Dockerfile에서 이걸 다시 빌드하면 이미지당 몇 시간이 걸리고, 버전 하나만 어긋나도
런타임에 세그폴트로 죽는다. **이미 동작하는 환경을 그대로 쓰는 것**이 옳다.

재현성은 `conda env export`로 남긴 스펙 파일이 담당한다(문서 역할).
환경 자체를 다른 머신으로 옮길 때는 `conda-pack`으로 재배치 가능한 tar를 만든다.

## 이미지는 왜 하나뿐인가

컨테이너는 셋인데 Dockerfile은 하나다. 환경·소스·가중치를 전부 마운트하므로
세 컨테이너의 차이가 **"어느 python으로 어떤 스크립트를 실행하는가"**밖에
남지 않기 때문이다. 그건 빌드가 아니라 런타임 설정이라
`docker-compose.gpu.yml`의 `command`와 환경변수로 정한다.

## boto3는 왜 이미지에 따로 넣는가

마운트한 conda 환경에는 boto3가 없다. 19GB짜리 환경을 수정하는 대신
`/opt/worker-deps`에 넣고 `PYTHONPATH`로 얹는다. 이때 `--no-deps`가 중요하다 —
boto3가 끌고 오는 `urllib3`·`python-dateutil`·`six`는 이미 환경에 있고,
PYTHONPATH는 site-packages보다 **우선**하므로 그것까지 깔면 환경이 검증해둔
버전을 가려버린다. 실제로 없는 4개(`boto3 botocore jmespath s3transfer`)만 넣는다.

## 왜 컨테이너를 셋으로 나누는가

uLayout(torch 2.2 / py3.10)과 omni3d(torch 2.2 / py3.10 + detectron2)와
sam3d(torch 2.5 / py3.11)는 의존성이 충돌해 한 파이썬 환경에 못 들어간다.
반면 **한 GPU 인스턴스 위에 컨테이너 셋을 올리는 건 아무 문제가 없다.**
로컬에서 conda 환경 셋으로 돌리던 구조가 컨테이너 셋으로 그대로 옮겨간다.

uLayout과 Omni3D는 이미 HTTP 사이드카(server.py, :8002/:8003)라서
컨테이너로 감싸기만 하면 된다. 통신 방식도 그대로다.

## 경로 규약

컨테이너 안에서는 호스트와 같은 절대 경로를 쓴다. conda 환경에는 생성 당시의
prefix가 스크립트와 설정에 박혀 있고, editable 설치는 소스 디렉터리의 절대
경로를 가리킨다. 경로를 맞춰주면 아무것도 고칠 필요가 없다.

운영에서는 conda-pack으로 prefix를 재배치한 뒤 `/opt/envs/...`로 옮긴다.

### editable 설치를 빠짐없이 마운트하는 법

환경만 마운트하면 되는 게 아니다. editable로 깔린 패키지는 site-packages에
**코드가 없고** 바깥 소스 디렉터리를 가리키는 포인터만 있다. 그걸 안 붙이면
`ModuleNotFoundError`가 난다. 짐작하지 말고 환경에 직접 물어본다.

```bash
for env in sam3d uLayout omni3d; do
  echo "=== $env ==="
  ~/miniconda3/envs/$env/bin/python - <<'EOF'
import os, re, site, glob
for sp in site.getsitepackages():
    for f in glob.glob(os.path.join(sp, "*.pth")) + glob.glob(os.path.join(sp, "__editable__*")):
        txt = open(f, encoding="utf-8", errors="ignore").read()
        for p in sorted({m.group(1) for m in re.finditer(r"(/home/[^/]+/[^/'\"\s,)]+)", txt)}):
            if "/miniconda3/" not in p:   # 환경 밖을 가리키는 것만이 마운트 대상
                print(f"  {os.path.basename(f)} -> {p}")
EOF
done
```

실제 결과다.

| 환경 | 가리키는 곳 |
|---|---|
| sam3d | `~/sam-3d-objects` |
| uLayout | (없음) |
| omni3d | `~/detectron2`, `~/pytorch3d_omni3d_build` |

omni3d의 pytorch3d는 `~/pytorch3d`가 **아니라** `~/pytorch3d_omni3d_build`다.
이름만 보고 마운트했으면 틀렸을 경로라, 이 조회를 꼭 거쳐야 한다.

### ~/.cache 는 이미지 안에 미리 만들어 둔다

`~/.cache/huggingface` 처럼 **하위** 경로만 마운트하면, docker가 없는 부모
`~/.cache` 를 **root 소유로** 자동 생성한다. 그러면 uid 1000으로 도는 앱이
그 안에 새 캐시 디렉터리를 만들지 못한다. 그래서 Dockerfile에서 먼저 만든다.

```dockerfile
RUN install -d -o ${APP_UID} -g ${APP_UID} /home/${APP_USER}/.cache
```

이 문제는 **첫 작업만 죽고 두 번째부터는 성공**하는 모습으로 나타나서
특히 고약하다(NVIDIA Warp가 `~/.cache/warp` 생성에 실패한 뒤 우회 경로를
탄다). 스케일투제로에서는 콜드스타트마다 첫 요청이 실패한다는 뜻이다.
