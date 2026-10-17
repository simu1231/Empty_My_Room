# 처리 시간 측정 결과

원시 데이터: `raw.csv` · 단위 초 · 측정 5회(워밍업 제외) · ±는 표본 표준편차

## 표 1. 장면별 처리 시간

| 장면 | 가구 수 | SAM2 | uLayout | LaMa | SD | SD 로드 | SAM3D(가구당) | 스케일 계산(Omni3D+SAM3D scale, 서버) | 전체(순차) | 전체(병렬) |
|---|---|---|---|---|---|---|---|---|---|---|
| sceneA | 2 | 0.59±0.03 | 2.37±0.13 | 0.19±0.02 | 6.16±0.07 | 2.84±0.11 | 69.39±67.42 | — | 151.31±134.92 | 56.08±5.40 |
| sceneB | 3 | 1.20±1.42 | 2.49±0.12 | 0.45±0.35 | 9.85±0.13 | 3.07±0.20 | 20.65±1.80 | 0.28±0.03 | 79.97±6.56 | 90.15±7.59 (n=4) |
| sceneC | 2 | 0.56±0.04 | 2.54±0.09 | 0.23±0.01 | 9.81±0.11 | 3.19±0.33 | 22.77±1.10 | — | 62.29±2.32 | 72.13±3.65 |
| sceneD | 1 | 0.35±0.07 | 2.30±0.10 | 0.12±0.01 | 4.33±0.13 | 2.85±0.12 | 19.20±0.63 | — | 29.55±0.66 | 29.97±1.07 |
| **전체 평균** | 8 | 0.67±0.73 | 2.43±0.15 | 0.25±0.21 | 7.54±2.45 | 2.99±0.25 | 33.00±37.74 | 0.28±0.03 (n=5) | 80.78±77.02 | 60.60±22.73 (n=19) |

전체 평균 행은 모든 장면의 측정값을 한데 모은 평균±표준편차다(장면 평균의 평균이 아니다). 가구 수가 다른 장면이 섞이므로 SAM3D(가구당)와 스케일 열만 가구 단위로 정규화돼 있다.

### 열 정의

- **SAM2** = `SAM2_mask`(클릭 전체 점으로 마스크 1개) + `SAM2_extract`(원본 사진에서 가구별 추출). 둘 다 SAM2 추론이다.
- **uLayout** = `/infer` + `/rectify` HTTP 왕복 시간 합(순차). 사이드카가 PerspectiveFields 를 요청마다 로드/해제하므로 로드가 포함돼 있고 분리할 수 없다.
- **SD** = 추론만. **SD 로드**는 `SDService()` 생성(`from_pretrained`)이며 요청마다 일어난다. 해제(`SD_unload`)는 표 3.
- **SAM3D(가구당)** = `generate()` + 응답 직렬화(orjson). 파이프라인은 상주(로드는 표 3). 이 값은 가구별 평균이다.
- **스케일 계산(Omni3D+SAM3D scale, 서버)** = Omni3D `/estimate` HTTP 왕복. SAM3D 포즈 디코더 scale 환산(`generate()` 마지막의 numpy 몇 줄)은 코드를 수정하지 않고는 분리할 수 없어 SAM3D 열에 포함돼 있다. **Omni3D 미지원 카테고리(침대·소파·의자 등)는 프런트도 호출하지 않으므로 이 열에 들어가지 않는다.**
- **프런트 JS의 카메라 포즈 기반 광선–바닥 교차는 측정에서 제외했다**(브라우저에서 실행되는 코드).
- **전체(순차)** = SAM2_mask 부터 마지막 가구의 SAM3D/Omni3D 까지의 wall-clock. 단계 합이 아니라 따로 잰 값이며 SD 로드/해제를 포함한다. SAM2/LaMa 의 세션 시작 로드(`load_*`)와 사용자 클릭 시간은 제외.
- **전체(병렬)** = 같은 흐름에서 uLayout `/infer`‖`/rectify` 와 가구별 SAM3D‖Omni3D 를 동시에 보낸 wall-clock.

## 표 2. 가구별 SAM3D / 스케일 / 병렬 구간

| 장면 | 가구 | SAM3D(순차) | Omni3D(순차) | 순차 합 | 병렬 구간 wall-clock |
|---|---|---|---|---|---|
| sceneA | 침대 | 38.61±20.71 | — | 38.61±20.71 | 23.10±0.94 |
| sceneA | 협탁 | 100.17±114.16 | — | 100.17±114.16 | 20.29±4.86 |
| sceneB | 소파 | 20.87±0.51 | — | 20.87±0.51 | 21.76±0.79 |
| sceneB | 스탠드 조명 | 23.08±6.04 | 0.30±0.03 | 23.37±6.03 | 29.54±6.98 (n=4) |
| sceneB | 액자 | 17.99±1.49 | 0.26±0.06 | 18.25±1.49 | 21.02±1.71 (n=4) |
| sceneC | 의자 | 19.86±0.64 | — | 19.86±0.64 | 30.53±3.47 |
| sceneC | 침대 | 25.67±1.67 | — | 25.67±1.67 | 25.20±0.56 |
| sceneD | 침대 | 19.20±0.63 | — | 19.20±0.63 | 19.04±0.70 |

## 표 3. 모델 로딩·해제 (추론 시간과 분리)

| 항목 | 값(초) | 비고 |
|---|---|---|
| SAM3D 파이프라인 로드 | 32.53 | 프로세스당 1회(라우터는 app.state 캐시, 워커는 프로세스 수명) |
| SD_load | 2.99±0.25 | 요청마다 로드(inpaint.py) |
| SD_unload | 0.22±0.01 | 요청마다 해제 |
| load_SAM2 | 0.86±0.08 | 세션 시작 시 재로드 — 앞 실행이 release 했기 때문 |
| load_LaMa | 0.31±0.03 | 위와 같음 |
| release_SAM2_LaMa | 0.18±0.00 | 명시적 해제 호출 |

## 표 4. 병렬 구간 (장면 합계)

| 장면 | uLayout 순차(infer+rectify) | uLayout 병렬 구간 | 가구 구간 순차 합(SAM3D+Omni3D) | 가구 구간 병렬 합 |
|---|---|---|---|---|
| sceneA | 2.37±0.13 | 2.37±0.11 | 138.78±134.84 | 43.39±5.26 |
| sceneB | 2.49±0.12 | 2.44±0.12 | 62.50±5.34 | 62.21±23.84 |
| sceneC | 2.54±0.09 | 2.27±0.06 | 45.54±2.21 | 55.73±3.66 |
| sceneD | 2.30±0.10 | 2.28±0.08 | 19.20±0.63 | 19.04±0.70 |

## 이상치 (평균에서 제외하지 않음)

| 장면 | 모드 | 단계 | 가구 | 반복 | 값 | 중앙값 |
|---|---|---|---|---|---|---|
| sceneA | sequential | SAM3D | 침대 | 4 | 69.46 | 25.46 |
| sceneA | sequential | SAM3D | 협탁 | 1 | 166.91 | 29.98 |
| sceneA | sequential | SAM3D | 협탁 | 4 | 270.12 | 29.98 |
| sceneA | sequential | e2e |  | 1 | 230.25 | 67.89 |
| sceneA | sequential | e2e |  | 4 | 352.16 | 67.89 |
| sceneB | sequential | SAM2_mask |  | 1 | 3.45 | 0.32 |
| sceneB | sequential | LaMa |  | 1 | 1.08 | 0.30 |
| sceneB | parallel | SAM2_mask |  | 4 | 3.27 | 0.51 |
| sceneB | parallel | SAM2_mask |  | 5 | 1.06 | 0.51 |
| sceneB | parallel | LaMa |  | 4 | 0.67 | 0.30 |
| sceneB | parallel | Scale_Omni3D | 스탠드 조명 | 3 | 10.47 | 0.48 |
| sceneB | parallel | SAM3D | 스탠드 조명 | 2 | 315.01 | 29.55 |
| sceneB | parallel | Scale_Omni3D | 액자 | 1 | 0.43 | 5.43 |
| sceneD | parallel | SAM2_mask |  | 1 | 2.63 | 0.21 |

## 실패 (3행) — 재실행하지 않았다

| 장면 | 반복 | phase | 모드 | 단계 | 가구 | 메시지 |
|---|---|---|---|---|---|---|
| sceneB | 2 | measure | parallel | Scale_Omni3D | 스탠드 조명 | ReadTimeout: HTTPConnectionPool(host='localhost', port=8003): Read timed out. (read timeout=120) |
| sceneB | 2 | measure | parallel | furniture_phase | 스탠드 조명 | ReadTimeout: HTTPConnectionPool(host='localhost', port=8003): Read timed out. (read timeout=120) |
| sceneB | 2 | measure | parallel | e2e |  | ReadTimeout: HTTPConnectionPool(host='localhost', port=8003): Read timed out. (read timeout=120) |

## 경고 (48행) — HTTP 는 성공했지만 응답이 success=false

- sceneA it=0 warmup sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneA it=0 warmup parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneA it=1 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneA it=1 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneA it=2 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneA it=2 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneA it=3 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneA it=3 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneA it=4 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneA it=4 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneA it=5 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneA it=5 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneB it=0 warmup sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneB it=0 warmup parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneB it=1 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneB it=1 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneB it=2 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneB it=2 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneB it=3 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneB it=3 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneB it=4 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneB it=4 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneB it=5 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneB it=5 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneC it=0 warmup sequential uLayout_rectify : api success=false: solvePnP 재투영오차 초과 (30.3px > 10.0px) — 포즈를 신뢰할 수 없습니다.
- sceneC it=0 warmup parallel uLayout_rectify : api success=false: solvePnP 재투영오차 초과 (30.3px > 10.0px) — 포즈를 신뢰할 수 없습니다.
- sceneC it=1 measure sequential uLayout_rectify : api success=false: solvePnP 재투영오차 초과 (30.3px > 10.0px) — 포즈를 신뢰할 수 없습니다.
- sceneC it=1 measure parallel uLayout_rectify : api success=false: solvePnP 재투영오차 초과 (30.3px > 10.0px) — 포즈를 신뢰할 수 없습니다.
- sceneC it=2 measure sequential uLayout_rectify : api success=false: solvePnP 재투영오차 초과 (30.3px > 10.0px) — 포즈를 신뢰할 수 없습니다.
- sceneC it=2 measure parallel uLayout_rectify : api success=false: solvePnP 재투영오차 초과 (30.3px > 10.0px) — 포즈를 신뢰할 수 없습니다.
- sceneC it=3 measure sequential uLayout_rectify : api success=false: solvePnP 재투영오차 초과 (30.3px > 10.0px) — 포즈를 신뢰할 수 없습니다.
- sceneC it=3 measure parallel uLayout_rectify : api success=false: solvePnP 재투영오차 초과 (30.3px > 10.0px) — 포즈를 신뢰할 수 없습니다.
- sceneC it=4 measure sequential uLayout_rectify : api success=false: solvePnP 재투영오차 초과 (30.3px > 10.0px) — 포즈를 신뢰할 수 없습니다.
- sceneC it=4 measure parallel uLayout_rectify : api success=false: solvePnP 재투영오차 초과 (30.3px > 10.0px) — 포즈를 신뢰할 수 없습니다.
- sceneC it=5 measure sequential uLayout_rectify : api success=false: solvePnP 재투영오차 초과 (30.3px > 10.0px) — 포즈를 신뢰할 수 없습니다.
- sceneC it=5 measure parallel uLayout_rectify : api success=false: solvePnP 재투영오차 초과 (30.3px > 10.0px) — 포즈를 신뢰할 수 없습니다.
- sceneD it=0 warmup sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneD it=0 warmup parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneD it=1 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneD it=1 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneD it=2 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneD it=2 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneD it=3 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneD it=3 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneD it=4 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneD it=4 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneD it=5 measure sequential uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
- sceneD it=5 measure parallel uLayout_rectify : api success=false: 코너 2개(뒷벽 정면 구도)를 검출하지 못했습니다 — 현재는 이 구도만 지원합니다.
