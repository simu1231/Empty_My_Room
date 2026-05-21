import { useRef, useEffect, useState, useCallback } from 'react'
import { useStore } from '../store/useStore'
import toast from 'react-hot-toast'

const LABEL_COLORS = [
  'rgba(255,80,80,0.45)', 'rgba(80,160,255,0.45)', 'rgba(80,255,120,0.45)',
  'rgba(255,200,80,0.45)', 'rgba(200,80,255,0.45)', 'rgba(80,255,220,0.45)',
  'rgba(255,120,200,0.45)', 'rgba(160,255,80,0.45)',
]

const FURNITURE_LIST = ['소파', '침대', '책상', '의자', '테이블', '옷장', '서랍장', 'TV', 'TV 거치대', '책장', '창문', '에어컨', '액자', '시계', '조명', '스탠드 조명', '화분', '커튼', '기타']

export default function SegmentStep() {
  const {
  originalFile, originalUrl,
  clickPoints, addPoint, removeLastPoint, clearPoints,
  setStep, setLoading, setMask, setFurnitureList, setEmptyRoom
  } = useStore()

  const canvasRef = useRef(null)
  const [selectedLabel, setSelectedLabel] = useState('소파')
  const [customLabel, setCustomLabel] = useState('')
  const [maskPreviews, setMaskPreviews] = useState({}) // label → base64 mask
  const [resizedImageB64, setResizedImageB64] = useState(null) // 첫 미리보기에서 저장한 리사이즈 이미지
  const [maskLoading, setMaskLoading] = useState(false)
  const debounceRef = useRef(null)

  const getCurrentLabel = () => {
    if (selectedLabel === '기타') return customLabel.trim() || '기타'
    return selectedLabel
  }

  useEffect(() => {
    if (!originalUrl) return
    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')
    const img = new Image()
    img.src = originalUrl
    img.onload = () => {
      canvas.width = img.width
      canvas.height = img.height
      ctx.drawImage(img, 0, 0)
    }
  }, [originalUrl])

  // 캔버스 다시 그리기 (이미지 + 마스크 오버레이 + 포인트)
  const redrawCanvas = useCallback(() => {
    if (!originalUrl) return
    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')
    const img = new Image()
    img.src = originalUrl
    img.onload = () => {
      ctx.drawImage(img, 0, 0)

      // 레이블별 색상으로 마스크 오버레이
      const labels = [...new Set(clickPoints.map(p => p.label))]
      let colorIdx = 0
      const labelColorMap = {}
      labels.forEach(l => { labelColorMap[l] = LABEL_COLORS[colorIdx++ % LABEL_COLORS.length] })

      const drawMasks = async () => {
        for (const [label, maskB64] of Object.entries(maskPreviews)) {
          if (!maskB64) continue
          const color = labelColorMap[label] || LABEL_COLORS[0]
          const maskImg = new Image()
          maskImg.src = `data:image/png;base64,${maskB64}`
          await new Promise(res => { maskImg.onload = res })
          // 마스크를 오프스크린 캔버스에 그려서 색상 적용
          const off = document.createElement('canvas')
          off.width = canvas.width; off.height = canvas.height
          const offCtx = off.getContext('2d')
          offCtx.drawImage(maskImg, 0, 0, canvas.width, canvas.height)
          const imgData = offCtx.getImageData(0, 0, canvas.width, canvas.height)
          const [r, g, b] = color.match(/\d+/g).map(Number)
          for (let i = 0; i < imgData.data.length; i += 4) {
            if (imgData.data[i] > 128) { // 마스크 흰 영역
              imgData.data[i] = r; imgData.data[i+1] = g; imgData.data[i+2] = b; imgData.data[i+3] = 140
            } else { imgData.data[i+3] = 0 }
          }
          offCtx.putImageData(imgData, 0, 0)
          ctx.drawImage(off, 0, 0)
        }

        // 포인트 그리기
        clickPoints.forEach((pt) => {
          const color = labelColorMap[pt.label] || 'rgba(255,80,80,0.9)'
          ctx.beginPath()
          ctx.arc(pt.x, pt.y, 10, 0, Math.PI * 2)
          ctx.fillStyle = color.replace('0.45', '0.9')
          ctx.fill()
          ctx.strokeStyle = 'white'
          ctx.lineWidth = 2
          ctx.stroke()
          ctx.fillStyle = 'white'
          ctx.font = 'bold 11px Arial'
          ctx.fillText(pt.label?.slice(0, 2) || '', pt.x - 6, pt.y + 4)
        })
      }
      drawMasks()
    }
  }, [clickPoints, originalUrl, maskPreviews])

  useEffect(() => { redrawCanvas() }, [redrawCanvas])

  const fetchMaskPreview = useCallback(async (label, labelPoints, file) => {
    if (!labelPoints.length || !file) return
    setMaskLoading(true)
    try {
      const form = new FormData()
      form.append('image', file)
      form.append('points', JSON.stringify(labelPoints.map(p => [p.x, p.y])))
      const res = await fetch('http://127.0.0.1:8001/api/segment/mask', { method: 'POST', body: form })
      const data = await res.json()
      if (data.success) {
        setMaskPreviews(prev => ({ ...prev, [label]: data.mask_b64 }))
        setResizedImageB64(prev => prev ?? data.resized_image_b64) // 처음 한 번만 저장
      }
    } catch (_) {}
    setMaskLoading(false)
  }, [])

  const handleClick = (e) => {
    const canvas = canvasRef.current
    const rect = canvas.getBoundingClientRect()
    const scaleX = canvas.width / rect.width
    const scaleY = canvas.height / rect.height
    const x = Math.round((e.clientX - rect.left) * scaleX)
    const y = Math.round((e.clientY - rect.top) * scaleY)
    const label = getCurrentLabel()
    addPoint({ x, y, label })

    // 300ms debounce 후 해당 레이블 마스크 미리보기
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => {
      const labelPoints = [...clickPoints.filter(p => p.label === label), { x, y, label }]
      fetchMaskPreview(label, labelPoints, originalFile)
    }, 300)
  }

  // 미리보기 마스크들을 OR 합산 (lighten 합성)
  const combineMaskPreviews = () => new Promise(resolve => {
    const masks = Object.values(maskPreviews).filter(Boolean)
    if (!masks.length) { resolve(null); return }
    const first = new Image()
    first.onload = () => {
      const off = document.createElement('canvas')
      off.width = first.naturalWidth; off.height = first.naturalHeight
      const ctx = off.getContext('2d')
      ctx.fillStyle = 'black'; ctx.fillRect(0, 0, off.width, off.height)
      ctx.globalCompositeOperation = 'lighten'
      let loaded = 0
      masks.forEach(b64 => {
        const img = new Image()
        img.onload = () => {
          ctx.drawImage(img, 0, 0)
          if (++loaded === masks.length) resolve(off.toDataURL('image/png').split(',')[1])
        }
        img.src = `data:image/png;base64,${b64}`
      })
    }
    first.src = `data:image/png;base64,${masks[0]}`
  })

  const handleExtract = async () => {
  if (clickPoints.length === 0) {
    toast.error('가구를 먼저 클릭해서 선택하세요!')
    return
  }
  setLoading(true, 'SAM2가 가구 영역 분석중...')
  try {
    // 모든 레이블 미리보기가 준비됐으면 SAM2 재호출 없이 재사용
    const labels = [...new Set(clickPoints.map(p => p.label))]
    const canReuse = resizedImageB64 && labels.every(l => maskPreviews[l])

    let maskB64, resizedB64
    if (canReuse) {
      console.log('[최적화] SAM2 재호출 스킵 — 미리보기 마스크 재사용')
      maskB64   = await combineMaskPreviews()
      resizedB64 = resizedImageB64
    } else {
      const form1 = new FormData()
      form1.append('image', originalFile)
      form1.append('points', JSON.stringify(clickPoints.map(p => [p.x, p.y])))
      form1.append('labels', JSON.stringify(clickPoints.map(p => p.label)))
      const res1 = await fetch('http://127.0.0.1:8001/api/segment/mask', { method: 'POST', body: form1 })
      const data1 = await res1.json()
      if (!data1.success) throw new Error('마스크 생성 실패')
      maskB64    = data1.mask_b64
      resizedB64 = data1.resized_image_b64
    }
    setMask(maskB64)

    // LaMa로 빈방 만들기
    setLoading(true, 'LaMa가 가구 제거중...')
    const maskBlob = await fetch(`data:image/png;base64,${maskB64}`).then(r => r.blob())
    const maskFile = new File([maskBlob], 'mask.png', { type: 'image/png' })
    const resizedBlob = await fetch(`data:image/png;base64,${resizedB64}`).then(r => r.blob())
    const resizedFile = new File([resizedBlob], 'resized.png', { type: 'image/png' })

    const form2 = new FormData()
    form2.append('image', resizedFile)
    form2.append('mask', maskFile)

    const res2 = await fetch('http://127.0.0.1:8001/api/inpaint/remove', {
      method: 'POST',
      body: form2,
    })
    const data2 = await res2.json()
    if (!data2.success) throw new Error('가구 제거 실패')

    const resultBlob = await fetch(`data:image/jpeg;base64,${data2.result_b64}`).then(r => r.blob())
    const resultUrl = URL.createObjectURL(resultBlob)
    const resultFile = new File([resultBlob], 'empty_room.jpg', { type: 'image/jpeg' })
    setEmptyRoom(resultUrl, resultFile)

    // 가구 추출
    setLoading(true, '가구 추출중...')
    const form3 = new FormData()
    form3.append('image', originalFile)
    form3.append('points', JSON.stringify(clickPoints.map(p => [p.x, p.y])))
    form3.append('labels', JSON.stringify(clickPoints.map(p => p.label || '기타')))

    const res3 = await fetch('http://127.0.0.1:8001/api/extract/furniture', {
      method: 'POST',
      body: form3,
    })
    const data3 = await res3.json()
    if (!data3.success) throw new Error('가구 추출 실패')

    setFurnitureList(data3.furniture)
    toast.success(`완료! 빈방 생성 + 가구 ${data3.furniture.length}개 추출 🎉`)
    setStep('roommaking')

  } catch (e) {
    toast.error(`오류 발생: ${e.message}`)
  } finally {
    setLoading(false)
  }
}

  const labelCounts = clickPoints.reduce((acc, pt) => {
    acc[pt.label] = (acc[pt.label] || 0) + 1
    return acc
  }, {})

  return (
    <div style={{ padding: '20px' }}>
      <h2>가구를 클릭해서 선택하세요</h2>
      <p style={{ color: '#aaa' }}>가구 종류 선택 후 해당 가구 위를 클릭하세요</p>

      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', margin: '16px 0' }}>
        {FURNITURE_LIST.map(label => (
          <button
            key={label}
            onClick={() => setSelectedLabel(label)}
            style={{
              padding: '8px 16px',
              background: selectedLabel === label ? '#3498db' : '#333',
              color: 'white',
              border: 'none',
              borderRadius: '20px',
              cursor: 'pointer',
              fontSize: '14px',
            }}
          >
            {label} {labelCounts[label] ? `(${labelCounts[label]})` : ''}
          </button>
        ))}
      </div>

      

      {selectedLabel === '기타' && (
        <div style={{ marginBottom: '16px' }}>
          <input
            type="text"
            value={customLabel}
            onChange={(e) => setCustomLabel(e.target.value)}
            placeholder="가구 이름을 입력하세요"
            style={{
              width: '300px',
              padding: '10px 14px',
              background: '#1a1a1a',
              border: '2px solid #3498db',
              borderRadius: '8px',
              color: 'white',
              fontSize: '14px',
              outline: 'none',
            }}
          />
        </div>
      )}

      <div style={{ display: 'flex', gap: '20px', marginTop: '10px' }}>
        <div style={{ flex: 1, position: 'relative' }}>
          <canvas
            ref={canvasRef}
            onClick={handleClick}
            style={{ width: '100%', cursor: 'crosshair', borderRadius: '12px', border: `2px solid ${maskLoading ? '#3498db' : '#333'}`, transition: 'border-color 0.2s' }}
          />
          {maskLoading && (
            <div style={{ position: 'absolute', top: '10px', right: '10px', background: 'rgba(0,0,0,0.7)', color: '#3498db', padding: '4px 10px', borderRadius: '12px', fontSize: '12px' }}>
              마스크 분석중...
            </div>
          )}
        </div>

        <div style={{ width: '220px', background: '#1a1a1a', borderRadius: '12px', padding: '20px', display: 'flex', flexDirection: 'column', height: '600px' }}>
          <h3 style={{ marginBottom: '12px' }}>선택된 포인트 {clickPoints.length > 0 && `(${clickPoints.length}개)`}</h3>
          <div style={{ flex: 1, overflowY: 'auto', marginBottom: '10px' }}>
            {clickPoints.length === 0 ? (
              <p style={{ color: '#666', fontSize: '14px' }}>가구 종류 선택 후 이미지를 클릭하세요</p>
            ) : (
              clickPoints.map((pt, i) => (
                <div key={i} style={{ background: '#2a2a2a', borderRadius: '8px', padding: '8px', marginBottom: '8px', fontSize: '13px' }}>
                  🔵 {pt.label}<br />
                  <span style={{ color: '#888' }}>x: {pt.x}, y: {pt.y}</span>
                </div>
              ))
            )}
          </div>
          <div>
              <button
                onClick={() => {
                  if (clickPoints.length === 0) return
                  const removed = clickPoints[clickPoints.length - 1]
                  removeLastPoint()
                  const remaining = clickPoints.slice(0, -1).filter(p => p.label === removed.label)
                  if (remaining.length === 0) {
                    setMaskPreviews(prev => { const n = { ...prev }; delete n[removed.label]; return n })
                  } else {
                    if (debounceRef.current) clearTimeout(debounceRef.current)
                    debounceRef.current = setTimeout(() => fetchMaskPreview(removed.label, remaining, originalFile), 300)
                  }
                }}
                disabled={clickPoints.length === 0}
                style={{ width: '100%', padding: '10px', marginBottom: '8px', background: clickPoints.length > 0 ? '#e67e22' : '#555', color: 'white', border: 'none', borderRadius: '8px', cursor: clickPoints.length > 0 ? 'pointer' : 'not-allowed' }}
              >
                ↩ 마지막 취소
              </button>
              <button onClick={() => { clearPoints(); setMaskPreviews({}); setResizedImageB64(null) }} style={{ width: '100%', padding: '10px', marginBottom: '8px', background: '#333', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer' }}>
                🗑️ 초기화
              </button>
              <button onClick={handleExtract} style={{ width: '100%', padding: '12px', background: '#3498db', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '15px', fontWeight: 'bold' }}>
                🪄 가구 추출
              </button>
            </div>
        </div>
      </div>
    </div>
  )
}
