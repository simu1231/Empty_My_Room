import { useRef, useEffect, useState, useCallback } from 'react'
import { useStore } from '../store/useStore'
import { API } from '../utils/api'
import toast from 'react-hot-toast'

export default function InpaintRefineStep() {
  const { emptyRoomUrl, emptyRoomFile, setEmptyRoom, setStep, setLoading } = useStore()

  const canvasRef = useRef(null)
  const [points, setPoints] = useState([])
  const [maskB64, setMaskB64] = useState(null)
  const [maskLoading, setMaskLoading] = useState(false)
  const [inpainting, setInpainting] = useState(false)
  const [currentFile, setCurrentFile] = useState(emptyRoomFile)
  const [currentUrl, setCurrentUrl] = useState(emptyRoomUrl)
  const currentFileRef = useRef(emptyRoomFile)  // 항상 최신 file 참조
  const debounceRef = useRef(null)

  // 캔버스에 현재 이미지 + 마스크 오버레이 그리기
  const redraw = useCallback(() => {
    if (!currentUrl || !canvasRef.current) return
    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')
    const img = new Image()
    img.src = currentUrl
    img.onload = async () => {
      canvas.width = img.width
      canvas.height = img.height
      ctx.drawImage(img, 0, 0)

      if (maskB64) {
        const mi = new Image()
        mi.src = `data:image/png;base64,${maskB64}`
        await new Promise(res => { mi.onload = res })
        const off = document.createElement('canvas')
        off.width = canvas.width; off.height = canvas.height
        const oc = off.getContext('2d')
        oc.drawImage(mi, 0, 0, canvas.width, canvas.height)
        const d = oc.getImageData(0, 0, canvas.width, canvas.height)
        for (let i = 0; i < d.data.length; i += 4) {
          if (d.data[i] > 128) { d.data[i] = 99; d.data[i+1] = 179; d.data[i+2] = 237; d.data[i+3] = 110 }
          else d.data[i+3] = 0
        }
        oc.putImageData(d, 0, 0)
        ctx.drawImage(off, 0, 0)
      }

      points.forEach(pt => {
        ctx.beginPath()
        ctx.arc(pt.x, pt.y, 8, 0, Math.PI * 2)
        ctx.fillStyle = 'rgba(99,179,237,0.9)'; ctx.fill()
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.stroke()
      })
    }
  }, [currentUrl, maskB64, points])

  useEffect(() => { redraw() }, [redraw])

  const fetchMask = useCallback(async (pts, file) => {
    if (!pts.length || !file || file.size === 0) return
    setMaskLoading(true)
    try {
      const form = new FormData()
      form.append('image', file)
      form.append('points', JSON.stringify(pts.map(p => [p.x, p.y])))
      const res = await fetch(API.segment, { method: 'POST', body: form })
      const data = await res.json()
      if (data.success) setMaskB64(data.mask_b64)
      else toast.error('마스크 생성 실패: ' + (data.error || '알 수 없는 오류'))
    } catch (e) {
      toast.error('마스크 요청 실패: 서버에 연결할 수 없습니다')
    } finally {
      setMaskLoading(false)
    }
  }, [])

  const handleClick = (e) => {
    const canvas = canvasRef.current
    const rect = canvas.getBoundingClientRect()
    const x = Math.round((e.clientX - rect.left) * canvas.width / rect.width)
    const y = Math.round((e.clientY - rect.top) * canvas.height / rect.height)
    const np = [...points, { x, y }]
    setPoints(np)
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => fetchMask(np, currentFileRef.current), 300)
  }

  const handleInpaint = async () => {
    if (!maskB64 || !currentFile) { toast.error('지울 영역을 먼저 클릭하세요'); return }
    setInpainting(true)
    setLoading(true, 'LaMa가 영역 제거중...')
    try {
      const maskBlob = await fetch(`data:image/png;base64,${maskB64}`).then(r => r.blob())
      const maskFile = new File([maskBlob], 'mask.png', { type: 'image/png' })
      const form = new FormData()
      form.append('image', currentFile)
      form.append('mask', maskFile)
      const res = await fetch(API.inpaint, { method: 'POST', body: form })
      const data = await res.json()
      if (!data.success) throw new Error(data.error || '인페인팅 실패')

      const blob = await fetch(`data:image/jpeg;base64,${data.result_b64}`).then(r => r.blob())
      const url = URL.createObjectURL(blob)
      const file = new File([blob], 'empty_room.jpg', { type: 'image/jpeg' })
      setCurrentUrl(url)
      setCurrentFile(file)
      currentFileRef.current = file  // ref 즉시 업데이트
      setEmptyRoom(url, file)
      setPoints([])
      setMaskB64(null)
      toast.success('제거 완료! 계속 클릭해서 추가 제거하거나 다음 단계로 이동하세요')
    } catch (e) {
      toast.error(`실패: ${e.message}`)
    } finally {
      setInpainting(false)
      setLoading(false)
    }
  }

  const handleUndo = () => {
    const np = points.slice(0, -1)
    setPoints(np)
    if (!np.length) setMaskB64(null)
    else { clearTimeout(debounceRef.current); debounceRef.current = setTimeout(() => fetchMask(np, currentFile), 300) }
  }

  return (
    <div style={{ padding: '20px', fontFamily: "'Inter', sans-serif", color: '#f0f0f0' }}>
      <h2 style={{ marginBottom: '6px' }}>빈방 정리</h2>
      <p style={{ color: '#888', marginBottom: '20px', fontSize: '14px' }}>
        지저분한 부분을 클릭해서 추가로 제거하세요. 만족하면 다음 단계로 이동하세요.
      </p>

      <div style={{ display: 'flex', gap: '20px' }}>
        {/* 캔버스 */}
        <div style={{ flex: 1, position: 'relative' }}>
          <canvas
            ref={canvasRef}
            onClick={handleClick}
            style={{ width: '100%', cursor: 'crosshair', borderRadius: '12px', border: `2px solid ${maskLoading ? '#3498db' : '#333'}` }}
          />
          {maskLoading && (
            <div style={{ position: 'absolute', top: 10, right: 10, background: 'rgba(0,0,0,0.7)', color: '#3498db', padding: '4px 10px', borderRadius: '12px', fontSize: '12px' }}>
              분석중...
            </div>
          )}
        </div>

        {/* 컨트롤 패널 */}
        <div style={{ width: '200px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
          <div style={{ background: '#1a1a1a', borderRadius: '12px', padding: '16px' }}>
            <p style={{ fontSize: '13px', color: '#aaa', marginBottom: '12px' }}>
              {points.length > 0 ? `${points.length}개 포인트 선택됨` : '제거할 영역을 클릭하세요'}
            </p>
            <button
              onClick={handleInpaint}
              disabled={!points.length || !maskB64 || inpainting}
              style={{
                width: '100%', padding: '12px', marginBottom: '8px',
                background: points.length > 0 && maskB64 ? '#3498db' : '#333',
                color: 'white', border: 'none', borderRadius: '8px',
                cursor: points.length > 0 && maskB64 ? 'pointer' : 'not-allowed',
                fontWeight: 600, fontSize: '14px'
              }}
            >
              {inpainting ? '제거중...' : '🪄 이 영역 제거'}
            </button>
            <button
              onClick={handleUndo}
              disabled={points.length === 0}
              style={{ width: '100%', padding: '10px', marginBottom: '8px', background: '#333', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
            >
              ↩ 마지막 취소
            </button>
            <button
              onClick={() => { setPoints([]); setMaskB64(null) }}
              style={{ width: '100%', padding: '10px', background: '#2a2a2a', color: '#aaa', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
            >
              🗑️ 포인트 초기화
            </button>
          </div>

          <button
            onClick={() => setStep('roommaking')}
            style={{
              width: '100%', padding: '14px',
              background: 'linear-gradient(135deg, #27ae60, #2ecc71)',
              color: 'white', border: 'none', borderRadius: '10px',
              cursor: 'pointer', fontWeight: 700, fontSize: '15px'
            }}
          >
            ✅ 완료 → 방 만들기
          </button>

          <button
            onClick={() => setStep('segment')}
            style={{ width: '100%', padding: '10px', background: '#2a2a2a', color: '#888', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
          >
            ← 가구 선택으로
          </button>
        </div>
      </div>
    </div>
  )
}
