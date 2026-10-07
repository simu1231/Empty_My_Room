/**
 * 비동기 작업(큐) 클라이언트.
 *
 * 왜 필요한가
 * ──────────
 * 배포 후 GPU 워커는 요청이 없으면 0대로 줄어든다(Scale-to-zero). 요청이 오면
 * 인스턴스를 새로 띄우고 모델을 올리느라 5분 안팎이 걸린다. 브라우저 fetch는
 * 그렇게 오래 열려 있지 못하고, 중간의 로드밸런서도 보통 60초 안에 끊는다.
 * 그래서 "요청을 보내고 결과를 기다린다"를 "작업을 접수하고(즉시 응답)
 * 결과를 주기적으로 물어본다"로 바꾼다.
 *
 * 무엇이 안 바뀌는가
 * ──────────────────
 * 워커가 S3에 올리는 결과 JSON은 기존 동기 API의 응답과 똑같은 모양이다.
 * 그래서 호출부는 결과를 받은 뒤의 코드를 한 줄도 바꿀 필요가 없다.
 */
import { API_BASE } from './api'

// 접수 서버(CPU 상시). CloudFront 가 /api/* 를 이 서버로 보내므로 프런트에서는
// 같은 출처의 상대경로면 된다 — 주소를 빌드 시점에 박아 둘 이유가 없어졌다.
// 로컬에서 바로 띄운 API 를 쓰려면 VITE_JOB_API_URL 로 덮는다.
export const JOB_API_BASE = import.meta.env.VITE_JOB_API_URL || ''

// 큐 경로를 쓸지, 기존 동기 API를 그대로 쓸지. 로컬에서 실제 모델을 돌릴 때는
// 꺼두고(기본), 배포 환경에서만 켠다. 3단계에서 워커에 실제 모델이 들어간 뒤
// 이 값을 1로 두면 같은 코드가 큐를 타게 된다.
export const USE_JOB_QUEUE = import.meta.env.VITE_USE_JOB_QUEUE === '1'

// 모델 이름 → (큐 job_type, 기존 동기 엔드포인트)
const MODELS = {
  sam3dMesh:      { jobType: 'sam3d_mesh',  legacy: `${API_BASE}/api/sam3d/mesh` },
  omni3dEstimate: { jobType: 'omni3d',      legacy: `${API_BASE}/api/omni3d/estimate` },
  roomLayout:     { jobType: 'room_layout', legacy: `${API_BASE}/api/room/layout` },
}

export class JobError extends Error {}

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  const t = setTimeout(resolve, ms)
  signal?.addEventListener('abort', () => { clearTimeout(t); reject(new JobError('취소됨')) }, { once: true })
})

/** 작업 접수. 즉시 job_id를 돌려준다. */
export async function submitJob(jobType, form, { signal } = {}) {
  form.append('job_type', jobType)
  const res = await fetch(`${JOB_API_BASE}/api/jobs`, { method: 'POST', body: form, signal })
  if (!res.ok) throw new JobError(`접수 실패(HTTP ${res.status}): ${await res.text()}`)
  return (await res.json()).job_id
}

/**
 * 완료될 때까지 상태를 물어본다.
 *
 * 간격을 고정하지 않고 늘려가는 이유: 짧은 작업은 빨리 알아채야 하고,
 * GPU가 깨어나길 기다리는 5분 동안은 굳이 1초마다 두드릴 필요가 없다.
 * 요청 수가 곧 API 서버 비용이다.
 */
export async function pollJob(jobId, { onProgress, signal, timeoutMs = 15 * 60 * 1000 } = {}) {
  const started = Date.now()
  let interval = 1000
  for (;;) {
    if (Date.now() - started > timeoutMs) throw new JobError('시간 초과: 작업이 끝나지 않았습니다')
    await sleep(interval, signal)
    interval = Math.min(interval * 1.4, 5000)

    const res = await fetch(`${JOB_API_BASE}/api/jobs/${jobId}`, { signal })
    if (res.status === 404) continue          // 레코드 전파 지연. 다음 차례에 다시 본다.
    if (!res.ok) continue                     // 일시적 오류로 작업을 포기하지 않는다.
    const job = await res.json()

    onProgress?.({ status: job.status, waitedSec: Math.round((Date.now() - started) / 1000) })

    if (job.status === 'done') {
      // 결과는 API 서버를 거치지 않고 S3에서 직접 받는다.
      const out = await fetch(job.result_url, { signal })
      if (!out.ok) throw new JobError(`결과 내려받기 실패(HTTP ${out.status})`)
      return await out.json()
    }
    if (job.status === 'failed') throw new JobError(job.error || '작업 실패')
  }
}

/**
 * 호출부가 쓰는 함수. 큐를 쓰든 안 쓰든 반환 모양이 같다.
 *
 *   const data = await callModel('sam3dMesh', form, { onProgress })
 *   if (!data.success) throw new Error(data.error)
 */
export async function callModel(name, form, { onProgress, signal } = {}) {
  const model = MODELS[name]
  if (!model) throw new JobError(`알 수 없는 모델: ${name}`)

  if (!USE_JOB_QUEUE) {
    const res = await fetch(model.legacy, { method: 'POST', body: form, signal })
    return await res.json()
  }

  const jobId = await submitJob(model.jobType, form, { signal })
  onProgress?.({ status: 'queued', waitedSec: 0 })
  return await pollJob(jobId, { onProgress, signal })
}

/** 진행 상태를 사람이 읽는 문구로. 콜드스타트를 오류로 오해하지 않게 한다. */
export function progressText({ status, waitedSec }) {
  if (status === 'queued') {
    return waitedSec < 20
      ? '작업 접수됨, 순서를 기다리는 중...'
      : `GPU를 준비하는 중입니다 (${waitedSec}초). 처음 요청은 몇 분 걸릴 수 있어요.`
  }
  if (status === 'running') return `처리 중... (${waitedSec}초)`
  return '대기 중...'
}
