import { useRef, useState, useEffect } from 'react'
import { useStore } from '../store/useStore'
import toast from 'react-hot-toast'

export default function SegmentStep() {
  const {
    originalUrl,
    clickPoints, addPoint, clearPoints,
    setStep, setLoading
  } = useStore()

  const canvasRef = useRef(null)

  // 이미지 캔버스에 그리기
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

  // 클릭 포인트 그리기
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
        ctx.font = 'bold 12px Arial'
        ctx.fillText(i + 1, pt.x - 4, pt.y + 4)
      })
    }
  }, [clickPoints, originalUrl])

  // 캔버스 클릭 핸들러
  const handleClick = (e) => {
    const canvas = canvasRef.current
    const rect = canvas.getBoundingClientRect()
    const scaleX = canvas.width / rect.width
    const scaleY = canvas.height / rect.height
    const x = Math.round((e.clientX - rect.left) * scaleX)
    const y = Math.round((e.clientY - rect.top) * scaleY)
    addPoint({ x, y })
    toast.success(`포인트 ${clickPoints.length + 1} 추가!`)
  }

  // 가구 제거 실행
  const handleRemove = async () => {
    if (clickPoints.length === 0) {
      toast.error('가구를 먼저 클릭해서 선택하세요!')
      return
    }
    setLoading(true, 'AI가 가구 영역을 분석중...')
    toast.success('백엔드 연결 후 실제 AI가 실행됩니다!')
    setLoading(false)
    setStep('result')
  }

  return (
    <div style={{ padding: '20px' }}>
      <h2>제거할 가구를 클릭하세요</h2>
      <p style={{ color: '#aaa' }}>
        이미지 위에서 제거하고 싶은 가구를 클릭하세요. 여러 개 클릭 가능해요.
      </p>

      <div style={{ display: 'flex', gap: '20px', marginTop: '20px' }}>

        {/* 캔버스 */}
        <div style={{ flex: 1 }}>
          <canvas
            ref={canvasRef}
            onClick={handleClick}
            style={{
              width: '100%',
              cursor: 'crosshair',
              borderRadius: '12px',
              border: '2px solid #333',
            }}
          />
        </div>

        {/* 사이드 패널 */}
        <div style={{
          width: '220px',
          background: '#1a1a1a',
          borderRadius: '12px',
          padding: '20px',
          display: 'flex',
          flexDirection: 'column',
          height: '600px',
        }}>
          <h3 style={{ marginBottom: '12px' }}>
            선택된 포인트 {clickPoints.length > 0 && `(${clickPoints.length}개)`}
          </h3>

          {/* 스크롤 가능한 포인트 리스트 */}
          <div style={{
            flex: 1,
            overflowY: 'auto',
            marginBottom: '10px',
          }}>
            {clickPoints.length === 0 ? (
              <p style={{ color: '#666', fontSize: '14px' }}>
                이미지를 클릭해서 가구를 선택하세요
              </p>
            ) : (
              clickPoints.map((pt, i) => (
                <div key={i} style={{
                  background: '#2a2a2a',
                  borderRadius: '8px',
                  padding: '8px',
                  marginBottom: '8px',
                  fontSize: '13px',
                }}>
                  🔴 포인트 {i + 1}<br />
                  <span style={{ color: '#888' }}>x: {pt.x}, y: {pt.y}</span>
                </div>
              ))
            )}
          </div>

          {/* 버튼들 - 항상 하단에 고정 */}
          <div>
            <button
              onClick={clearPoints}
              style={{
                width: '100%',
                padding: '10px',
                marginBottom: '8px',
                background: '#333',
                color: 'white',
                border: 'none',
                borderRadius: '8px',
                cursor: 'pointer',
              }}
            >
              🗑️ 초기화
            </button>

            <button
              onClick={handleRemove}
              style={{
                width: '100%',
                padding: '12px',
                background: '#e74c3c',
                color: 'white',
                border: 'none',
                borderRadius: '8px',
                cursor: 'pointer',
                fontSize: '15px',
                fontWeight: 'bold',
              }}
            >
              🪄 가구 제거
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}