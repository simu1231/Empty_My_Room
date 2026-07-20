import { useState } from 'react'
import { useStore } from '../store/useStore'
import { API, b64ToFile } from '../utils/api'
import toast from 'react-hot-toast'

export default function RoomMakingStep() {
  const {
    emptyRoomUrl, emptyRoomFile, maskB64,
    setStep, setRoomSize, setRoomMesh, setRoomColors, setRoomSurfaceTextures, setRoomBoxTextures, setLoading, reset
  } = useStore()

  const [width,  setW] = useState(4.5)
  const [depth,  setD] = useState(3.5)
  const [height, setH] = useState(2.5)
  const [preview, setPreview] = useState(null)   // { wallColor, floorColor, wallTex, floorTex }
  const [analyzing, setAnalyzing] = useState(false)
  const [layoutAuto, setLayoutAuto] = useState(false)   // 치수가 자동 추정된 값인지

  const handleAnalyze = async () => {
    if (!emptyRoomFile) return
    setAnalyzing(true)
    setLoading(true, '빈방 이미지에서 색상·텍스처·레이아웃 분석 중...')
    try {
      const colorForm = new FormData()
      colorForm.append('image', emptyRoomFile)
      // 가구/창문/커튼을 지울 때 쓴 SAM2 마스크 — 있으면 패치 후보 선정 시 제외 영역으로 재사용
      if (maskB64) colorForm.append('mask', b64ToFile(maskB64, 'mask.png', 'image/png'))
      const layoutForm = new FormData()
      layoutForm.append('image', emptyRoomFile)
      const rectifyForm = new FormData()
      rectifyForm.append('image', emptyRoomFile)

      const [colorRes, layoutRes, rectifyRes] = await Promise.allSettled([
        fetch(`${API.generate3d.replace('generate3d', 'extract-colors')}`, { method: 'POST', body: colorForm }),
        fetch(API.layout, { method: 'POST', body: layoutForm }),
        fetch(API.rectifyTextures, { method: 'POST', body: rectifyForm }),
      ])

      if (colorRes.status !== 'fulfilled') throw new Error('색상 분석 요청 실패')
      const data = await colorRes.value.json()
      if (!data.success) throw new Error(data.error || '분석 실패')
      setPreview({
        wallColor:  data.wall_color,
        floorColor: data.floor_color,
        wallTex:    data.wall_texture,
        floorTex:   data.floor_texture,
        wallPatch:  data.wall_patch,   // 타일링용 균일 패치 (RANSAC 실패 시 기본 폴백)
        floorPatch: data.floor_patch,
      })

      // 레이아웃(방 치수) 자동 추정 — 실패해도 색상 분석 결과는 그대로 사용, 수동 입력값 유지
      if (layoutRes.status === 'fulfilled') {
        const layoutData = await layoutRes.value.json()
        if (layoutData.success) {
          const d = layoutData.room_dimensions_m
          setW(d.room_width_m.toFixed(2))
          setD(d.room_depth_m.toFixed(2))
          setH(d.room_height_m.toFixed(2))
          setLayoutAuto(true)
          toast.success('색상 분석 + 방 치수 자동 추정 완료!')
        } else {
          setLayoutAuto(false)
          toast.success('색상 분석 완료! (치수는 수동 입력해주세요)')
        }
      } else {
        setLayoutAuto(false)
        toast.success('색상 분석 완료! (치수는 수동 입력해주세요)')
      }

      // 벽/바닥/천장 실사 텍스처 rectify — 실패해도 위 분석 결과는 그대로 사용,
      // Interior3DStep이 procedural 단색/패턴으로 폴백함
      if (rectifyRes.status === 'fulfilled') {
        const rectifyData = await rectifyRes.value.json()
        if (rectifyData.success) {
          setPreview(p => ({ ...p, boxTextures: rectifyData.textures }))
        }
      }
    } catch (e) {
      toast.error('분석 실패: ' + e.message)
    } finally {
      setAnalyzing(false)
      setLoading(false)
    }
  }

  const handleStart = () => {
    if (!preview) { toast.error('먼저 방을 분석해주세요'); return }
    setRoomSize({ width: parseFloat(width), depth: parseFloat(depth), height: parseFloat(height) })
    setRoomColors({ wall: preview.wallColor, floor: preview.floorColor })
    setRoomSurfaceTextures({ wall: preview.wallPatch, floor: preview.floorPatch })
    setRoomBoxTextures(preview.boxTextures || null)
    setRoomMesh(null)   // SAM3D 메시 없이 Three.js 박스 방 사용
    setStep('interior3d')
  }

  const toRgb = (arr) => arr ? `rgb(${Math.round(arr[0]*255)},${Math.round(arr[1]*255)},${Math.round(arr[2]*255)})` : '#888'

  return (
    <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start', fontFamily: "'Inter','Segoe UI',sans-serif", color: '#f0f0f0', padding: '4px 0' }}>

      {/* 빈방 이미지 */}
      <div style={{ flex: 1, borderRadius: 12, overflow: 'hidden', border: '1px solid #222', background: '#111', position: 'relative' }}>
        {emptyRoomUrl
          ? <img src={emptyRoomUrl} alt="empty room" style={{ width: '100%', display: 'block', maxHeight: 560, objectFit: 'contain' }} />
          : <div style={{ height: 300, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#444' }}>빈방 이미지 없음</div>
        }
        {/* 색상 오버레이 미리보기 */}
        {preview && (
          <div style={{ position: 'absolute', bottom: 12, left: 12, display: 'flex', gap: 8 }}>
            <div style={{ background: 'rgba(0,0,0,0.7)', borderRadius: 10, padding: '8px 12px', display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
              <div style={{ width: 20, height: 20, borderRadius: 4, background: toRgb(preview.wallColor), border: '1px solid rgba(255,255,255,0.2)' }} />
              <span style={{ color: '#ccc' }}>벽</span>
            </div>
            <div style={{ background: 'rgba(0,0,0,0.7)', borderRadius: 10, padding: '8px 12px', display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
              <div style={{ width: 20, height: 20, borderRadius: 4, background: toRgb(preview.floorColor), border: '1px solid rgba(255,255,255,0.2)' }} />
              <span style={{ color: '#ccc' }}>바닥</span>
            </div>
          </div>
        )}
      </div>

      {/* 오른쪽 패널 */}
      <div style={{ width: 220, flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 12 }}>

        {/* 텍스처 미리보기 */}
        {preview && (
          <div style={{ background: '#111', border: '1px solid #222', borderRadius: 12, padding: '14px', display: 'flex', gap: 8 }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 10, color: '#555', marginBottom: 4 }}>벽 텍스처</div>
              <img src={`data:image/jpeg;base64,${preview.wallTex}`} style={{ width: '100%', borderRadius: 6, objectFit: 'cover', height: 70 }} alt="wall" />
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 10, color: '#555', marginBottom: 4 }}>바닥 텍스처</div>
              <img src={`data:image/jpeg;base64,${preview.floorTex}`} style={{ width: '100%', borderRadius: 6, objectFit: 'cover', height: 70 }} alt="floor" />
            </div>
          </div>
        )}

        {/* 방 크기 */}
        <div style={{ background: '#111', border: '1px solid #222', borderRadius: 12, padding: '14px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
            <div style={{ fontSize: 11, color: '#444', letterSpacing: '0.08em', textTransform: 'uppercase' }}>방 크기 (m)</div>
            {layoutAuto && (
              <span style={{ fontSize: 10, color: '#6bcB77', background: 'rgba(107,203,119,0.12)', padding: '2px 6px', borderRadius: 6 }}>
                자동 추정
              </span>
            )}
          </div>
          {[['가로', width, setW], ['세로', depth, setD], ['높이', height, setH]].map(([label, val, set]) => (
            <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <label style={{ fontSize: 12, color: '#555', width: 30 }}>{label}</label>
              <input type="number" value={val} onChange={e => { set(e.target.value); setLayoutAuto(false) }} min="1" max="20" step="0.5"
                style={{ flex: 1, padding: '6px 8px', background: '#0a0a0a', border: '1px solid #222', borderRadius: 7, color: '#c0c0d0', fontSize: 13, outline: 'none' }} />
              <span style={{ fontSize: 11, color: '#333' }}>m</span>
            </div>
          ))}
        </div>

        {/* 분석 버튼 */}
        <button onClick={handleAnalyze} disabled={analyzing || !emptyRoomFile} style={{
          width: '100%', padding: '12px', borderRadius: 10, border: 'none',
          background: emptyRoomFile ? 'linear-gradient(135deg, #5b6eff, #9b5cff)' : '#1a1a1a',
          color: emptyRoomFile ? '#fff' : '#333', fontSize: 14, fontWeight: 600,
          cursor: emptyRoomFile ? 'pointer' : 'not-allowed', opacity: analyzing ? 0.6 : 1,
        }}>
          {analyzing ? '분석 중...' : '🎨 방 색상 분석'}
        </button>

        {/* 시작 버튼 */}
        <button onClick={handleStart} disabled={!preview} style={{
          width: '100%', padding: '13px', borderRadius: 10, border: 'none',
          background: preview ? 'linear-gradient(135deg, #27ae60, #2ecc71)' : '#1a1a1a',
          color: preview ? '#fff' : '#333', fontSize: 14, fontWeight: 700,
          cursor: preview ? 'pointer' : 'not-allowed',
        }}>
          {preview ? '✅ 3D 인테리어 배치 시작 →' : '먼저 분석을 실행하세요'}
        </button>

        <button onClick={() => setStep('refine')} style={{
          width: '100%', padding: '10px', background: 'transparent', color: '#555',
          border: '1px solid #222', borderRadius: 8, cursor: 'pointer', fontSize: 13,
        }}>
          ← 빈방 정리로
        </button>
      </div>
    </div>
  )
}
