import { useRef, useEffect, useState } from 'react'
import { useStore } from '../store/useStore'
import toast from 'react-hot-toast'

const FURNITURE_LIST = ['소파', '침대', '책상', '의자', '테이블', '옷장', '서랍장', 'TV', 'TV 거치대', '책장', '액자', '기타']

export default function SegmentStep() {
  const {
    originalFile, originalUrl,
    clickPoints, addPoint, clearPoints,
    setStep, setLoading, setMask, setFurnitureList
  } = useStore()

  const canvasRef = useRef(null)
  const [selectedLabel, setSelectedLabel] = useState('소파')
  const [customLabel, setCustomLabel] = useState('')

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

  useEffect(() => {
    if (!originalUrl) return
    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')
    const img = new Image()
    img.src = originalUrl
    img.onload = () => {
      ctx.drawImage(img, 0, 0)
      clickPoints.forEach((pt, i) => {
        ctx.beginPath()
        ctx.arc(pt.x, pt.y, 10, 0, Math.PI * 2)
        ctx.fillStyle = 'red'
        ctx.fill()
        ctx.fillStyle = 'white'
        ctx.font = 'bold 11px Arial'
        ctx.fillText(pt.label?.slice(0, 2) || (i + 1), pt.x - 6, pt.y + 4)
      })
    }
  }, [clickPoints, originalUrl])

  const handleClick = (e) => {
    const canvas = canvasRef.current
    const rect = canvas.getBoundingClientRect()
    const scaleX = canvas.width / rect.width
    const scaleY = canvas.height / rect.height
    const x = Math.round((e.clientX - rect.left) * scaleX)
    const y = Math.round((e.clientY - rect.top) * scaleY)
    const label = getCurrentLabel()
    addPoint({ x, y, label })
    toast.success(`${label} 포인트 추가!`)
  }

  const handleExtract = async () => {
    if (clickPoints.length === 0) {
      toast.error('가구를 먼저 클릭해서 선택하세요!')
      return
    }
    setLoading(true, 'SAM2가 가구 영역 분석중...')
    try {
      // SAM2로 마스크 생성
      const form1 = new FormData()
      form1.append('image', originalFile)
      form1.append('points', JSON.stringify(clickPoints.map(p => [p.x, p.y])))
      form1.append('labels', JSON.stringify(clickPoints.map(p => p.label)))

      const res1 = await fetch('http://127.0.0.1:8001/api/segment/mask', {
        method: 'POST',
        body: form1,
      })
      const data1 = await res1.json()
      if (!data1.success) throw new Error('마스크 생성 실패')
      setMask(data1.mask_b64)

      // 가구 추출
      setLoading(true, '가구 추출중...')
      const form2 = new FormData()
      form2.append('image', originalFile)
      form2.append('points', JSON.stringify(clickPoints.map(p => [p.x, p.y])))
      form2.append('labels', JSON.stringify(clickPoints.map(p => p.label || '기타')))

      const res2 = await fetch('http://127.0.0.1:8001/api/extract/furniture', {
        method: 'POST',
        body: form2,
      })
      const data2 = await res2.json()
      if (!data2.success) throw new Error('가구 추출 실패')

      setFurnitureList(data2.furniture)
      toast.success(`가구 ${data2.furniture.length}개 추출 완료! 🎉`)
      setStep('roomsetup')

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
        <div style={{ flex: 1 }}>
          <canvas
            ref={canvasRef}
            onClick={handleClick}
            style={{ width: '100%', cursor: 'crosshair', borderRadius: '12px', border: '2px solid #333' }}
          />
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
            <button onClick={clearPoints} style={{ width: '100%', padding: '10px', marginBottom: '8px', background: '#333', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer' }}>
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
