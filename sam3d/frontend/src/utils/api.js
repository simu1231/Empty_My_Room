import axios from 'axios'

/**
 * 2단계(대화형) 요청의 주소.
 *
 * 예전에는 브라우저가 GPU 워커의 :8001 을 직접 불렀다(VITE_API_URL). 그러려면
 * 바뀌는 워커 IP 를 빌드 시점에 알아야 하는데, 스케일투제로라 알 수가 없다.
 * 이제는 **같은 출처의 /api/gpu/** 로 보내고 API 서버가 워커로 넘긴다.
 *
 * 같은 출처라서 CORS 가 통째로 사라진다 — 프리플라이트도, 허용 목록도 없다.
 * 로컬 개발에서 백엔드를 직접 띄워 쓰고 싶으면 VITE_API_URL 로 덮는다.
 */
export const API_BASE = import.meta.env.VITE_API_URL || '/api/gpu'

// 접수 서버(상시 가동)가 직접 처리하는 것들. GPU 를 거치지 않으므로
// API_BASE 가 아니라 /api 아래에 둔다 — 색 뽑자고 GPU 를 깨우면 안 된다.
export const CPU_API = {
  extractColors: '/api/extract-colors',
  prewarm:       '/api/prewarm',
  gpuStatus:     '/api/gpu/status',
}

export const API = {
  segment:    `${API_BASE}/api/segment/mask`,
  segmentRelease: `${API_BASE}/api/segment/release`,
  inpaint:    `${API_BASE}/api/inpaint/remove`,
  extract:    `${API_BASE}/api/extract/furniture`,
  generate3d: `${API_BASE}/api/room/generate3d`,
  sam3dMesh:  `${API_BASE}/api/sam3d/mesh`,
  layout:     `${API_BASE}/api/room/layout`,
  rectifyTextures: `${API_BASE}/api/room/rectify_textures`,
  omni3dEstimate: `${API_BASE}/api/omni3d/estimate`,
  // 색 추출은 CPU 로 옮겼다. 예전에 호출부가
  // API.generate3d.replace('generate3d','extract-colors') 로 주소를 만들던
  // 자리다 — 경로가 바뀌면 조용히 404 가 되는 방식이라 항목으로 승격한다.
  extractColors: CPU_API.extractColors,
}

// 28초. CloudFront 오리진 응답 한도가 30초이고 API 서버는 25초에 포기한다.
// 그래서 **프런트가 가장 늦게** 포기해야 한다 — 먼저 포기하면 서버가 보내준
// 503 + Retry-After(= "얼마나 더 기다려라")를 못 읽고 그냥 실패로 본다.
// 예전 120초는 "GPU 가 깨어날 때까지 붙들고 있기" 전제의 값이라 더는 맞지 않다.
const api = axios.create({
  baseURL: API_BASE,
  timeout: 28000,
})

// base64 → blob URL 변환
export function b64ToUrl(b64, mime = 'image/jpeg') {
  const bin = atob(b64)
  const arr = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i)
  const blob = new Blob([arr], { type: mime })
  return URL.createObjectURL(blob)
}

// ── GPU 준비 대기 ──────────────────────────────────────────────────────
// GPU 가 0대면 서버가 503 + Retry-After 를 준다. 그건 오류가 아니라 "아직"
// 이라는 뜻이다. 호출부마다 이걸 따로 처리하면 어딘가는 반드시 빠뜨리고,
// 그 화면에서만 콜드스타트가 빨간 에러로 보인다. 그래서 한 곳에 둔다.

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  const t = setTimeout(resolve, ms)
  signal?.addEventListener('abort',
    () => { clearTimeout(t); reject(new DOMException('취소됨', 'AbortError')) },
    { once: true })
})

// 최대 10분. 0대에서 첫 요청까지가 7분쯤이라 그보다 넉넉해야 하고,
// 무한이면 영영 안 뜨는 상황에서 사용자가 아무 신호도 못 받는다.
const READY_TIMEOUT_MS = 10 * 60 * 1000

/**
 * fetch 와 같은 자리에 넣는다. 503 이면 Retry-After 만큼 쉬고 다시 보낸다.
 *
 *   const res = await gpuFetch(API.segment, { method: 'POST', body: form },
 *                              { onStatus: (s) => setMsg(s.message) })
 *
 * 200 이든 400 이든 503 이 아닌 응답은 그대로 돌려준다 — 이 함수는 기다림만
 * 담당하고 결과 해석은 호출부의 일이다.
 */
export async function gpuFetch(url, init = {}, { onStatus, signal } = {}) {
  const started = Date.now()
  for (;;) {
    const res = await fetch(url, { ...init, signal })
    if (res.status !== 503) return res

    // 본문이 우리 규약(state/phase/eta_sec)인지 본다. CloudFront 가 만든
    // 503 일 수도 있는데, 그건 JSON 이 아니다.
    let info = null
    try { info = await res.clone().json() } catch { /* 규약 밖 503 */ }
    if (!info?.phase) return res

    if (Date.now() - started > READY_TIMEOUT_MS) return res

    const waitedSec = Math.round((Date.now() - started) / 1000)
    onStatus?.({ ...info, waitedSec })

    const after = Number(res.headers.get('Retry-After')) || 10
    await sleep(after * 1000, signal)
  }
}

/** GPU 준비 상태를 사람이 읽는 문구로. 콜드스타트를 오류로 오해하지 않게 한다. */
export function readyText({ phase, eta_sec, waitedSec = 0 } = {}) {
  const left = Math.max(0, (eta_sec || 0) - waitedSec)
  const tail = left > 0 ? ` (약 ${left}초 남음)` : ''
  if (phase === 'instance_boot') return `GPU 서버를 켜는 중입니다${tail}`
  if (phase === 'models')        return `모델을 불러오는 중입니다${tail}`
  if (phase === 'sd')            return `인페인팅 모델을 준비하는 중입니다${tail}`
  if (phase === 'upstream')      return 'GPU 서버가 아직 응답하지 않습니다'
  return '준비 중입니다'
}

/**
 * 사진을 고른 순간 GPU 를 미리 깨운다.
 *
 * 그 뒤 사용자는 가구를 클릭해 마스크를 찍느라 최소 수십 초를 쓴다. 그 시간이
 * 부팅과 겹치면 체감 대기가 그만큼 줄어든다. 실패는 무시한다 — 선반입은
 * 최적화지 전제가 아니고, 실패해도 첫 요청이 알아서 깨운다.
 */
export function prewarmGpu() {
  fetch(CPU_API.prewarm, { method: 'POST' }).catch(() => {})
}

// SAM2 마스크 생성
export async function segmentMask(imageFile, points) {
  const form = new FormData()
  form.append('image', imageFile)
  form.append('points', JSON.stringify(points))
  const { data } = await api.post('/api/segment/mask', form)
  return data
}

// LaMa 가구 제거
export async function inpaintRemove(imageFile, maskFile) {
  const form = new FormData()
  form.append('image', imageFile)
  form.append('mask', maskFile)
  const { data } = await api.post('/api/inpaint/remove', form)
  return data
}
