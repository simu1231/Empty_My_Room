# 처리 시간 측정 (논문 VI장 표용)

기존 파이프라인 코드와 임계값은 수정하지 않는다. `sam3d/backend/services/*` 를 import 해서 쓰고,
사이드카(uLayout, Omni3D)는 HTTP 로 부른다.

## 파일

| 파일 | 역할 |
|---|---|
| `measure_timing.py` | 측정 본체. 장면당 워밍업 1 + 측정 5, 모드 2종(sequential / parallel) |
| `summarize_timing.py` | `raw.csv` → `table.md` (장면별 평균±표준편차, 전체 행, 보조 표, 이상치/실패) |
| `env_report.py` | GPU·드라이버·CUDA·PyTorch·Python 버전, GPU 점유 상태 |
| `scenes.json` | 장면 경로, 가구(한글 카테고리), 클릭 좌표(원본 해상도) |

사진은 `eval/timing/scenes/`, 결과는 `eval/timing_results/<타임스탬프>/` 에 쌓인다.

## 실행

```bash
# 0) 측정 전 :8001 백엔드는 내린다(SAM2/LaMa 가 GPU 를 이중 점유). uLayout :8002, Omni3D :8003 은 떠 있어야 한다.
#    `scripts/services.sh stop` 은 전체를 내리므로 쓰지 말 것.
# 1) 좌표 검증: SAM2 마스크 오버레이만 저장 (시간 측정 안 함)
~/miniconda3/envs/sam3d/bin/python tools/timing/measure_timing.py --preview-only
# 2) 측정
~/miniconda3/envs/sam3d/bin/python tools/timing/measure_timing.py [--only sceneA,sceneB] [--repeats 5] [--warmup 1]
# 3) 표만 다시 만들 때
~/miniconda3/envs/sam3d/bin/python tools/timing/summarize_timing.py eval/timing_results/<ts>/raw.csv
```

산출물: `raw.csv`, `table.md`, `env_before.txt`, `env_after.txt`, `run.log`, `masks/*.png`, `scenes.json`(사본).

## 측정 정의

실제 호출 순서를 따른다.
`SAM2_mask → LaMa → SD(로드/추론/해제) → SAM2_extract → SAM2/LaMa 해제 → uLayout → 가구마다 SAM3D·Omni3D`

- 각 단계는 `time.perf_counter()` 로 재고, GPU 단계는 전후에 `torch.cuda.synchronize()` 를 부른다.
- **전체(e2e)** 는 단계 합이 아니라 따로 잰 wall-clock 이다. 참고용 `stage_sum` 행이 있어 차이를 볼 수 있다.
- **모델 로딩은 추론과 분리**한다. `load_SAM2`, `load_LaMa`, `load_SAM3D`, `SD_load`, `SD_unload` 가 각각 행으로 남는다.
  SAM2/LaMa 는 앞 실행이 해제(release)하므로 매 실행 시작에 재로드되며, 이 시간은 e2e 에 넣지 않았다.
  SD 로드/해제는 실제 시스템도 요청마다 하므로 e2e 에 포함되고 `SD 로드` 열로도 따로 보고한다.
- 사용자 클릭 시간은 없다(좌표를 입력으로 준다). HTTP 업로드/응답 직렬화는 직접 import 단계에서는 제외, 사이드카 단계에서는 포함.

### 병렬 구간 (mode=parallel)

프런트가 실제로 동시에 보내는 곳 두 군데를 재현한다.
1. uLayout `/infer` ‖ `/rectify` (`RoomMakingStep.jsx`)
2. 가구마다 SAM3D ‖ Omni3D (`Interior3DStep.jsx` 의 `omni3dPromise`)

단계 열 측정(표 1)은 모두 sequential 행에서 가져온다. parallel 행은 `전체(병렬)` 열과 표 2·4 에만 쓰인다.

### 스케일 계산

`Omni3D /estimate` HTTP 왕복 = 열 `스케일 계산(Omni3D+SAM3D scale, 서버)`.
SAM3D 포즈 디코더 scale 환산은 `sam3d_runner.generate()` 마지막의 numpy 몇 줄이라 코드를 고치지 않고는 분리되지 않아 SAM3D 열에 포함된다.
프런트는 Omni3D 지원 카테고리(`스탠드 조명, 조명, 액자, 그림, 거울, 커튼`)만 호출하므로 침대·소파·의자·협탁은 이 열에 없다(`skipped` 행).

**측정 제외: 프런트 JS 의 카메라 포즈 기반 광선–바닥 교차.** 브라우저에서 실행되는 코드이며 서버 시간이 아니다.

## 알려진 한계

- `routers/extract.py`, `routers/inpaint.py` 의 로직은 함수로 분리돼 있지 않아 같은 순서로 재현했다. 라우터를 고치면 `measure_timing.py` 도 맞춰야 한다.
- `LamaService.inpaint()` 가 `/tmp/lama_debug.png` 를 저장한다. 그 시간이 LaMa 에 포함된다(코드를 고치지 않음).
- uLayout 사이드카는 PerspectiveFields 를 요청마다 로드/해제하므로 로드 시간을 분리할 수 없다.
- Omni3D 에는 프런트가 보내는 것과 같이 원본 해상도 파일을 보낸다(프런트가 리사이즈하는지는 확인하지 못했다).
- 실패·이상치는 조용히 재실행하지 않는다. 실패는 `status=failed` 행, 이상치는 `table.md` 의 표시로 남는다. 이상치도 평균에서 빼지 않는다.
- SAM2 후보 마스크 중 면적이 가장 큰 것을 고르는 규칙(extract.py) 때문에 비슷한 물체가 모여 있으면 합쳐진다(예: sceneA 의 펜던트 조명 2개 + 화병). 그런 가구는 측정 대상에서 뺐다.
