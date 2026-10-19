# AWS 는 fal, 로컬 PC 는 로컬 GPU — 그리고 g6.xlarge 시험

## 한 줄 요약

- **로컬 PC**: 바뀐 것 없음. `SAM3D_BACKEND` 가 없으면 `local`(기존 GPU 추론).
- **AWS(`deploy/aws/*`)**: 기본값이 `fal`. 되돌리려면 `EMR_SAM3D_BACKEND=local`.
- **GPU 인스턴스 타입**: 운영 기본은 계속 `stable`(g6.2xlarge, g5.2xlarge). g6.xlarge 는
  `trial-g6xl` 프로파일로만 켜지고, **측정으로 통과하기 전에는 운영 확정이 아니다.**

## 환경변수(이름 / 기본값)

| 이름 | 기본값 | 쓰이는 곳 |
|---|---|---|
| `EMR_SAM3D_BACKEND` | `fal` | AWS 스크립트(config.sh → userdata, smoke-test) |
| `EMR_GPU_PROFILE` | `stable` | `stable` = `g6.2xlarge g5.2xlarge` / `trial-g6xl` = `g6.xlarge` / `trial-g4dn2xl` = `g4dn.2xlarge` (trial 은 fal 에서만 허용) |
| `EMR_TRIAL_ACK` | 없음 | trial 프로파일을 ASG 에 적용할 때 `1` 필요 |
| `EMR_APPLY` | 없음 | `35-set-instance-types.sh` 가 실제로 바꾸려면 `1` (없으면 계획만) |
| `EMR_FAL_KEY_PARAM` | `/sam3d/FAL_KEY` | SSM SecureString 이름 |
| `E2E_SAM3D` | `SAM3D_BACKEND` 를 따름 | `local` / `fal` / `skip` (smoke·test_e2e) |
| `E2E_SAM3D_RUNS` | fal 1 · local 3 | fal 호출 횟수(요금) |
| `EMR_MEASURE` | `1` | smoke 중 자원 측정 켜기/끄기 |
| `EMR_MEASURE_ENFORCE` | stable `0` / trial 프로파일 `1` | 측정 판정을 smoke 종료 코드에 반영(아래 "측정 방법") |
| `EMR_MEASURE_MIN_SAMPLES` 등 | `10` 등 | 측정 판정 기준 조정: `_MIN_AVAIL_MIB`(1500) `_MAX_SWAP_MIB`(100) `_MAX_PSI_FULL`(1.0) `_MAX_PSI_SOME`(10) `_IOPS_CAP`(3000) `_MAX_IOPS_FRAC`(0.8) `_MAX_VRAM_FRAC`(0.9) |
| `EMR_GPU_COMPAT` | `1` | smoke 시작 전 GPU CUDA 커널 호환 점검(`check_gpu_compat.py`) |
| `EMR_BUILDER_TYPES` | `g6.xlarge g5.xlarge g6e.xlarge` | 빌더 타입. g4dn 을 시험하려면 `g4dn.2xlarge` 로 덮는다 |
| `SAM3D_FAL_*`, `FAL_KEY` | `sam3d_fal.py` 머리말 참고 | 코드 |

## fal 모드에서 바뀌는 부팅 동작

- userdata 가 SSM 에서 `FAL_KEY` 를 읽어 `.env` 에 쓴다(`set +x`, 변수에 안 담음, 실패하면 중단).
- `.env` 에 `WARM_GATE_FILE=`(빈 값)을 써서 sam3d 워커의 워밍 게이트를 끈다.
- 워밍에서 SAM3D 가중치(약 13GiB)를 부팅 때 걸러낸다(`EMR_WARM_SKIP_BOOT`). **AMI 에 구운 목록은
  그대로**라서 local 로 되돌리면 워밍이 다시 켜진다.
- 부팅 마지막에 워커 컨테이너의 실제 `SAM3D_BACKEND` 를 읽어, fal 인데 아니면(옛 AMI) 기동을 중단한다.
- 로컬 SAM3D 코드·가중치·conda 환경은 지우지 않는다(롤백, `/api/room/generate3d`).

## g6.xlarge 에 대해 알려진 것과 모르는 것

- 알려진 것: SAM3D 포함 구성의 VRAM 피크 20,359 MiB / RAM 피크(콜드 RSS) 15,238 MB → g6.xlarge(15,368 MB)에서
  스래싱(2026-10-07 실측). 그래서 local 은 xlarge 에서 쓸 수 없고, trial 은 fal 에서만 허용한다.
- **모르는 것**: SAM3D 를 뺀 구성(backend + uLayout + Omni3D + 워커들)의 **Host RAM**. VRAM 이 약 19GB
  줄어드는 것은 RAM 과 별개의 이야기다. 측정 전에는 g6.xlarge 가 된다고 말할 수 없다.
- vCPU 가 4개라 사이드카 둘 + 백엔드의 CPU 경합도 측정 대상이다(미측정).

## 후보 비교 (서울, 2026-10-18 읽기 전용 조회)

사양은 `describe-instance-types`, 스팟은 `describe-spot-price-history`(최근 7일 평균, AZ 합산)다.
온디맨드 가격은 이 계정에 Pricing API 권한이 없어 조회하지 못했다(저장소의 `cost_model.py` 값만 있다).

| 타입 | GPU(VRAM) | RAM | vCPU | 서울 제공 AZ | 스팟 7일 평균 | g6.2xlarge 대비 | 100 인스턴스-시간당 |
|---|---|---|---|---|---|---|---|
| g6.2xlarge (기준) | L4 22,888MiB | 32GiB | 8 | 2a 2c 2d | $0.5547 (범위 0.548~0.566) | — | $55.5 |
| **g6.xlarge** | L4 22,888MiB | 16GiB | 4 | 2a 2c 2d | $0.4678 | −15.7% | $46.8 |
| **g4dn.2xlarge** | T4 16,384MiB | 32GiB | 8 | 2a 2b 2c | $0.4494 | −19.0% | $44.9 |
| g4dn.xlarge | T4 16,384MiB | 16GiB | 4 | 2a 2b 2c | 약 $0.32(2시간 창, 7일 평균 아님) | 약 −42% | 약 $32 |
| g5.2xlarge | A10G 22,888MiB | 32GiB | 8 | 2a 2c 2d | 약 $0.69 | +24% | 약 $69 |

읽는 법
- 절감폭은 **시간당 단가**일 뿐이다. 실제 비용은 부팅 시간과 유휴 꼬리(`IDLE_EXIT_SEC` 900초)가 더 크게 좌우한다
  (cost_model.py 머리말). T4 가 L4 보다 느리면(미측정) 같은 작업에 인스턴스 시간이 더 든다 — 절감이 줄거나 사라질 수 있다.
- **g4dn.2xlarge** 는 RAM 이 기준과 같은 32GiB 라 RAM 위험은 작지만, VRAM 이 16GiB 이고 T4(sm_75)다.
  SAM3D 를 뺀 구성의 VRAM 관찰값(4090 기준 SD +2.85GB, SAM2+LaMa +1.27GB, 사이드카 둘 ≤2.5GB)을 단순히 더하면
  약 6.6GB 이지만 **합산은 틀린다**(캐싱 할당자·CUDA 컨텍스트) — 측정 필요.
- **g4dn.xlarge** 는 RAM 16GiB + VRAM 16GiB 라 두 가지 위험이 겹친다. 저렴해 보여도 검증 전이라 프로파일에 넣지 않았다.
- AZ: g6 는 {2a,2c,2d}, g4dn 은 {2a,2b,2c}. 교집합 {2a,2c} 만 두 계열을 같이 쓸 수 있다.
  `35-set-instance-types.sh` 는 현재 ASG 서브넷 중 새 타입을 파는 AZ 만 남긴다(g4dn 이면 2d 가 빠진다).
- vCPU 쿼터: 둘 다 G/VT 스팟 vCPU 로 센다. 2xlarge 는 8 vCPU, xlarge 는 4 vCPU.
- 드라이버: 빌더가 쓰는 Base OSS Nvidia Driver DLAMI 가 T4(Turing)를 지원한다는 것은 일반 지식이고 **미검증**이다
  (g4dn 빌더를 띄워 `nvidia-smi` 로 확인).

### CUDA 호환 — 파일만 봐서는 확정할 수 없다

- PyTorch 휠(torch 2.2/2.5)은 sm_75 를 포함한다(`torch.cuda.get_arch_list()` 로 확인).
- omni3d 환경의 `detectron2._C`, `pytorch3d._C` 는 개발 PC 에서 직접 빌드한 확장이다. `pytorch3d._C` 에는 sm_75
  문자열이 있지만 SASS 인지 PTX 인지, `detectron2._C` 는 아예 판별하지 못했다(`cuobjdump` 없음).
- 그래서 **그 GPU 에서 실제로 불러 보는** `check_gpu_compat.py` 를 만들었고, `smoke-test.sh` 가 스택을 올리기 전에
  세 환경(sam3d, omni3d, uLayout)에 대해 돌린다. 이 PC 의 4090(sm_89)에서는 전부 통과했다 — **T4(sm_75)·L4 결과가 아니다.**

## 새 AMI 검증과 시작 템플릿·시험 타입 안전장치

**왜**: 옛 AMI 에 fal userdata 를 붙이면 컨테이너가 `SAM3D_BACKEND` 를 못 받아 **조용히 local 로** 돈다. g6.xlarge 에서는
그때 RAM 이 모자라 스래싱한다. AMI ID 가 새것이라는 사실로는 내용을 알 수 없으므로 **FalReady 태그**로 검증한다.

**FalReady 태그** (`fal-ready.sh`)
- 값 = fal 관련 파일 9개(`bash deploy/aws/fal-ready.sh files`)의 **git blob id**(HEAD 기준)를 이어 붙인 sha256 앞 16자.
  AMI 를 굽던 코드가 지금 배포하려는 코드와 같을 때만 일치한다. 이 파일들이 나중에 바뀌면(이미지에 구워진 코드가
  바뀐 것이므로) 해시가 달라져 새 AMI 를 요구한다.
- 태그를 붙이는 곳은 **빌더의 `verify-ami.sh` 가 출력하는 `create-image` 명령 하나뿐**이다. 그 명령은 빌더 안에서
  `fal_check_ami_local` 이 ① fal 파일에 커밋 안 된 수정이 없는지 ② `emr/gpu:<태그>` 이미지에 `/opt/fal-deps/fal_client` 와
  `worker.py` 의 `run_sam3d` 가 있는지 ③ 체크아웃의 compose 가 `SAM3D_BACKEND` 를 전달하고 `WARM_GATE_FILE` 비활성화를
  지원하는지 ④ `run_sam3d` / `sam3d_fal.py` 가 있는지를 **직접 검사해 전부 통과한 뒤에만** 태그를 포함해 출력한다.
  실패하면 태그 없이 출력하고 "fal 모드 LT 에 쓸 수 없다"고 알린다.
- 한계: 그 명령 밖에서 사람이 손으로 붙인 태그는 막지 못한다.

**어디서 확인하나**
| 스크립트 | 동작 |
|---|---|
| `20-launch-template.sh` (fal 모드) | AMI 의 FalReady 가 없거나 HEAD 해시와 다르거나 조회에 실패하면 **LT 버전을 만들기 전에** 중단. local 모드(롤백)는 검사 없음 |
| `35-set-instance-types.sh` (trial) | ASG 가 실제로 쓸 LT 버전(`$Default` 등을 풀어서)의 userdata 에 **기능 표식 + fal 렌더 + SSM 복호화 읽기 + `SAM3D_BACKEND=fal` + 게이트 끔 + 옛 AMI 차단 가드**가 전부 있고, AMI 가 `available` 이며 FalReady 가 HEAD 와 일치해야 한다. 조회 실패·누락·불일치는 ASG 변경 전 중단(계획 출력에서도). 표식만 있거나 fal 설정만 있으면 통과하지 않는다 |
| `35-set-instance-types.sh` (stable 롤백) | **막지 않는다**(조회가 실패해도 계속). 대신 지금 적용되는 LT 버전·AMI·GitSha·FalReady·userdata 모드를 출력하고, fal userdata + 미검증 AMI 면 경고한다. 롤백은 LT 를 바꾸지 않는다 |
| userdata(부팅) | fal 인데 워커 컨테이너의 실제 `SAM3D_BACKEND` 가 fal 이 아니면(옛 AMI) 기동 중단 |

> **현재 상태 주의(2026-10-19 읽기 전용 조회)**: 기본 LT 버전 v19 는 옛 AMI(`def0123`)에 fal userdata(안전장치 없음)가 붙은
> 조합이다 — 워커가 조용히 local 로 돈다. 새 AMI + 새 LT 버전이 만들어지기 전에는 trial 적용이 위 검사로 막힌다.
> 이전 상태(v18: 같은 AMI + 옛 userdata)로 되돌리려면 `aws ec2 modify-launch-template --launch-template-name emr-gpu-worker --default-version 18`.

## 측정 방법

`smoke-test.sh` 가 자동으로 측정한다(`measure-resources.sh` → `measure_resources.py`):
시스템 RAM(MemAvailable), 스왑 증가량, PSI(메모리 압력), 디스크 읽기 IOPS, VRAM, 컨테이너별 메모리를
2초 간격으로 쌓고, 구간(`stack-up`, `e2e`, 작업별)별 최악값과 판정표를 `report.md` 로 만든다.
판정 기준(MemAvailable ≥ 1500MiB, PSI full ≤ 1%, IOPS ≤ 80% of 3000, VRAM ≤ 90% 등)은 **초안**이다.

**측정 결과를 smoke 성공/실패에 반영하기 — `EMR_MEASURE_ENFORCE`**
- `0`: 측정은 보여 주기만 한다(기존 동작). 기준 미달이어도 종료 코드는 e2e 결과만 따른다. **기본값은 stable 프로파일에서 0.**
- `1`: 측정 결과가 **없거나**(샘플 없음, 측정기 비정상 종료, `EMR_MEASURE_MIN_SAMPLES`(10)보다 짧음, 측정 시작 실패)
  **기준 미달**이면, e2e 가 통과했어도 종료 코드가 0 이 아니다(**3 = 기준 미달, 4 = 측정 없음/오류**). **기본값은 trial 프로파일에서 1.**
  `EMR_MEASURE=0` 과 함께 쓰면 시작 전에 거부한다.
- 출력 마지막의 **「최종 판정」** 블록이 `작업 검증(e2e)` 와 `자원 측정` 을 **따로** 보여 준다. 위쪽의
  "✔ 작업 검증(e2e)만 통과" 줄은 측정을 말하지 않는다. 강제가 아닌데 측정이 미달/오류면 "전체: 성공 — 단 측정이 …
  종료 코드에 반영되지 않았다" 로 표시한다.
- **운영 시 사용법**: 시험 타입(g6.xlarge / g4dn.2xlarge)의 RAM·VRAM 을 보는 smoke 는 `EMR_GPU_PROFILE=trial-…` 을 주거나
  `EMR_MEASURE_ENFORCE=1` 로 돌린다. stable 타입의 평소 스모크는 기본(0)으로 둬도 된다.

**운영 확정 전에 모두 필요한 것**
1. g6.xlarge 에서 fal 모드 스모크가 통과(`E2E_SAM3D=fal`, uLayout, Omni3D 포함).
2. `report.md` 의 모든 기준 PASS — 스모크를 **2회** 돌리고 2회차를 본다(1회차는 볼륨 지연 로딩으로 IOPS 가 부풀 수 있다).
3. 동시 호출·SD 로드/해제를 포함한 반복(세션 3회 이상)에서도 PASS.
4. 스팟 용량(서울 2a/2c/2d)에서 g6.xlarge 가 실제로 잡히는지 확인.
5. 위를 통과해도 한 번의 측정이다. 운영 타입을 바꾸는 결정은 별도로 한다.

> 워커(ASG)에는 SSH 가 없어서 호스트에서 측정기를 못 돌린다. RAM 측정은 **빌더(g6.xlarge, SSH 가능)에서 smoke** 로
> 한다. 빌더가 g6.xlarge 이므로 같은 타입이다. ASG 카나리는 부팅/흐름(CloudWatch 로그의 `[fal]`, `[worker]`)만 확인한다.

## AWS 에서 실행할 명령(이 저장소 작업에서는 실행하지 않았다)

```bash
# 0) 개발 PC — 권한(무료). fal 키는 SSM /sam3d/FAL_KEY 에 이미 있다.
bash deploy/aws/10-iam.sh

# 1) 커밋·푸시(별도 커밋). 빌더가 GitHub 에서 받는다.

# 2) 빌더(기존 AMI 기반, 스팟 g6.xlarge ≈ $0.48/h) — launch-builder.sh 안내문을 따른다
EMR_BASE_AMI=ami-02cd8c0e85d2a2b64 bash deploy/aws/launch-builder.sh
#   빌더 안에서(접속 즉시): sudo shutdown -h +360
#   cd /opt/emr/Empty_My_Room && git fetch origin && git checkout -q <새 커밋>
#   cd deploy/aws && bash bake-ami.sh
#   bash smoke-test.sh            # fal 모드 기본. 1회차
#   bash smoke-test.sh            # 2회차(캐시 데워진 상태) — 이 report.md 를 본다
#   bash verify-ami.sh            # fal 코드 검사 후 FalReady 태그가 든 create-image 명령을 출력 → 개발 PC 에서 실행
#                                 # (태그가 빠져 있으면 검사가 실패한 것: 위 항목을 고치고 bake-ami.sh 부터 다시)
#   → 끝나면 bash deploy/aws/launch-builder.sh terminate

# 3) 시작 템플릿 새 버전(fal 기본). AMI 의 FalReady 태그가 HEAD 와 일치해야만 만들어진다. 기본 버전이 즉시 바뀐다 — 롤백은 아래.
EMR_AMI_ID=<새 AMI> EMR_SG_ID=sg-... bash deploy/aws/20-launch-template.sh

# 4) 타입 시험(ASG 가 0/0 일 때만, 측정 통과 후): 계획 → 적용. 프로파일은 trial-g6xl | trial-g4dn2xl
#    LT 버전의 userdata·AMI(FalReady)가 검증되지 않으면 계획 출력에서도 중단한다
EMR_GPU_PROFILE=trial-g6xl EMR_TRIAL_ACK=1 bash deploy/aws/35-set-instance-types.sh
EMR_GPU_PROFILE=trial-g6xl EMR_TRIAL_ACK=1 EMR_APPLY=1 bash deploy/aws/35-set-instance-types.sh
```

g4dn.2xlarge 를 시험하려면 **빌더도 g4dn.2xlarge 로** 띄워야 그 GPU 에서 호환 점검과 RAM 측정이 된다
(8 vCPU 라 스팟 쿼터 8 을 다 쓴다. ASG 가 0 일 때만):

```bash
EMR_BUILDER_TYPES="g4dn.2xlarge" EMR_BASE_AMI=<새 AMI 또는 기존 AMI> bash deploy/aws/launch-builder.sh
#   빌더 안: cd /opt/emr/Empty_My_Room/deploy/aws && bash smoke-test.sh   # compat → 스택 → fal e2e → 측정 리포트
```

## 롤백

```bash
# 타입: stable(g6.2xlarge g5.2xlarge)로 복귀 — ASG 0/0 일 때
EMR_APPLY=1 bash deploy/aws/35-set-instance-types.sh
# 시작 템플릿: 이전 기본 버전으로
aws ec2 modify-launch-template --launch-template-name emr-gpu-worker --default-version 19
# SAM3D 를 AWS 에서 로컬로(RAM 30GB 필요 = g6.2xlarge 이상): 다시 렌더
EMR_SAM3D_BACKEND=local EMR_AMI_ID=... EMR_SG_ID=... bash deploy/aws/20-launch-template.sh
```

## 예상 비용(근거 표시)

| 항목 | 값 | 근거 |
|---|---|---|
| 빌더 스팟 g6.xlarge | 약 $0.48/h, 3~4시간 ≈ $1.5~2 | `launch-builder.sh` 안내문 |
| 빌더를 끄지 않은 경우 | 하룻밤 약 $24 | 같은 스크립트 주석. `shutdown -h +360` 필수 |
| g6.xlarge 스팟 | $0.4535/h | `cost_model.py` 2026-09-30 실측 |
| g6.2xlarge 스팟 | $0.5547/h(7일 평균) | `describe-spot-price-history` 2026-10-18 |
| g6.xlarge / g4dn.2xlarge 스팟 | $0.4678 / $0.4494 /h(7일 평균) | 같은 조회. 온디맨드는 조회 권한 없어 미확인 |
| g4dn.2xlarge 빌더(스팟) | 약 $0.45/h × 3~4시간 | 위 스팟 평균. 8 vCPU 라 쿼터 전부 사용 |
| fal 호출 | **단가 미확인** | 스모크 1회당 `E2E_SAM3D_RUNS`(기본 1)회 |
| 새 AMI 스냅샷 보관 | 월 보관료 추가 | 증분 크기 미확인. 안정화 후 옛 AMI 정리 |
| 카나리 g6.xlarge 1대 | 부팅+테스트+유휴 꼬리 `IDLE_EXIT_SEC`(900초) | 꼬리 비용이 타입 차이보다 클 수 있다 |

## 커밋 분리 범위(e8f16b2 는 그대로, 이번 변경은 별도 커밋)

수정: `deploy/aws/{config.sh,20-launch-template.sh,userdata.sh,smoke-test.sh,verify-ami.sh}`,
`deploy/docker-compose.aws.yml`, `deploy/test_e2e.py`
신규: `deploy/aws/{35-set-instance-types.sh,measure-resources.sh,measure_resources.py,check_gpu_compat.py,fal-ready.sh,TRIAL_G6XL.md}`,
`deploy/tests/test_aws_fal_deploy.py`
제외(커밋하지 않음): `deploy/*.whl`, `docs/`, `eval/`, `landing-page-export/`, `tools/timing/` 신규 3개.
