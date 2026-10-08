import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * 로컬 개발용 프록시.
 *
 * 배포에서는 CloudFront 가 /api/* 를 접수 서버로 보내고, 접수 서버가
 * /api/gpu/* 의 접두사를 떼어 GPU 워커(:8001)로 넘긴다. 로컬에는 그 접수
 * 서버가 없어서, 프록시가 없으면 /api/gpu/... 가 Vite 의 SPA 폴백에 걸려
 * **HTTP 200 + text/html** 로 돌아온다. 404 가 아니라 200 이라 호출부는
 * 성공으로 보고, res.json() 에서야 터진다 — 실패가 조용해지는 경로다.
 *
 * 그래서 접수 서버가 하던 주소 변환을 여기서 그대로 흉내 낸다.
 */
const BACKEND = 'http://127.0.0.1:8001'   // sam3d (SAM2/LaMa/SD/room/omni3d)

// 배포에만 있는 접수 서버 전용 경로. 로컬에선 GPU 가 항상 켜져 있으므로
// 깨울 것도, 기다릴 것도 없다. 폴백에 걸려 html 을 받느니 여기서 끝낸다.
function localApiShim() {
  const json = (res, body) => {
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify(body))
  }
  return {
    name: 'emr-local-api-shim',
    configureServer(server) {
      server.middlewares.use('/api/gpu/status', (_req, res) =>
        json(res, { state: 'ready', phase: 'ready', eta_sec: 0, sd_warm: true }))
      server.middlewares.use('/api/prewarm', (_req, res) => json(res, { ok: true }))
    },
  }
}

export default defineConfig({
  plugins: [react(), localApiShim()],
  server: {
    proxy: {
      // 접수 서버가 떼던 /api/gpu 접두사를 똑같이 뗀다.
      '/api/gpu': { target: BACKEND, changeOrigin: true,
                    rewrite: (p) => p.replace(/^\/api\/gpu/, '') },
      // 색 추출은 배포에서 CPU(접수 서버)가 처리한다. 로컬엔 그 구현이
      // 없고 같은 일을 하는 라우트가 백엔드에 있어 거기로 보낸다.
      '/api/extract-colors': { target: BACKEND, changeOrigin: true,
                               rewrite: () => '/api/room/extract-colors' },
    },
  },
})
