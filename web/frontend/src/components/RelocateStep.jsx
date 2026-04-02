import { useRef, useEffect, useState } from 'react'
import { useStore } from '../store/useStore'
import toast from 'react-hot-toast'

export default function RelocateStep() {
  const { originalFile, inpaintedUrl, clickPoints, reset } = useStore()

  const canvasRef = useRef(null)
  const [furnitureList, setFurnitureList] = useState([])
  const [selectedFurniture, setSelectedFurniture] = useState(null)
  const [placedItems, setPlacedItems] = useState([])
  const [dragging, setDragging] = useState(null)
  const [loading, setLoading] = useState(false)

  // 가구 추출
  useEffect(() => {
    if (!originalFile || !clickPoints.length) return
    extractFurniture()
  }, [])

  const extractFurniture = async () => {
    setLoading(true)
    try {
      const form = new FormData()
      form.append('image', originalFile)
      form.append('points', JSON.stringify(clickPoints.map(p => [p.x, p.y])))

      const res  = await fetch('http://127.0.0.1:8000/api/extract/furniture', {
        method: 'POST',
        body: form,
      })
      const data = await res.json()
      if (!data.success) throw new Error('추출 실패')

      setFurnitureList(data.furniture)
      toast.success(`가구 ${data.furniture.length}개 추출 완료!`)
    } catch (e) {
      toast.error(`가구 추출 실패: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  // 캔버스에 배경 이미지 그리기
  useEffect(() => {
    if (!inpaintedUrl) return
    const canvas = canvasRef.current
    const ctx    = canvas.getContext('2d')
    const img    = new Image()
    img.src      = inpaintedUrl
    img.onload   = () => {
      canvas.width  = img.width
      canvas.height = img.height
      ctx.drawImage(img, 0, 0)
      drawPlacedItems(ctx, img)
    }
  }, [inpaintedUrl, placedItems])

  const drawPlacedItems = (ctx, bgImg) => {
    ctx.drawImage(bgImg, 0, 0)
    placedItems.forEach(item => {
      const img    = new Image()
      img.src      = `data:image/png;base64,${item.b64}`
      img.onload   = () => {
        ctx.drawImage(img, item.x, item.y, item.width, item.height)
      }
    })
  }

  // 드래그 시작
  const handleDragStart = (furniture) => {
    setSelectedFurniture(furniture)
  }

  // 캔버스에 드롭
  const handleCanvasDrop = (e) => {
    if (!selectedFurniture) return
    const canvas = canvasRef.current
    const rect   = canvas.getBoundingClientRect()
    const scaleX = canvas.width  / rect.width
    const scaleY = canvas.height / rect.height
    const x = Math.round((e.clientX - rect.left) * scaleX)
    const y = Math.round((e.clientY - rect.top)  * scaleY)

    const newItem = {
      ...selectedFurniture,
      x: x - 100,
      y: y - 100,
      width:  200,
      height: 200,
    }
    setPlacedItems(prev => [...prev, newItem])
    setSelectedFurniture(null)
    toast.success('가구 배치 완료!')
  }

  return (
    <div style={{ padding: '20px' }}>
      <h2>가구 재배치</h2>
      <p style={{ color: '#aaa' }}>왼쪽 가구를 드래그해서 방에 배치하세요</p>

      <div style={{ display: 'flex', gap: '20px', marginTop: '20px' }}>

        {/* 가구 목록 */}
        <div style={{
          width: '160px',
          background: '#1a1a1a',
          borderRadius: '12px',
          padding: '12px',
          overflowY: 'auto',
          maxHeight: '600px',
        }}>
          <h3 style={{ marginBottom: '12px', fontSize: '14px' }}>추출된 가구</h3>
          {loading ? (
            <p style={{ color: '#666', fontSize: '13px' }}>추출 중...</p>
          ) : furnitureList.length === 0 ? (
            <p style={{ color: '#666', fontSize: '13px' }}>가구 없음</p>
          ) : (
            furnitureList.map((f, i) => (
              <div
                key={i}
                draggable
                onDragStart={() => handleDragStart(f)}
                style={{
                  background: '#2a2a2a',
                  borderRadius: '8px',
                  padding: '8px',
                  marginBottom: '8px',
                  cursor: 'grab',
                  border: selectedFurniture?.id === f.id ? '2px solid #e74c3c' : '2px solid transparent',
                }}
              >
                <img
                  src={`data:image/png;base64,${f.b64}`}
                  style={{ width: '100%', borderRadius: '4px' }}
                />
                <p style={{ fontSize: '11px', color: '#888', marginTop: '4px', textAlign: 'center' }}>
                  가구 {i + 1}
                </p>
              </div>
            ))
          )}
        </div>

        {/* 캔버스 */}
        <div style={{ flex: 1 }}>
          <canvas
            ref={canvasRef}
            onDragOver={(e) => e.preventDefault()}
            onDrop={handleCanvasDrop}
            style={{
              width: '100%',
              borderRadius: '12px',
              border: '2px solid #333',
            }}
          />
        </div>

        {/* 컨트롤 */}
        <div style={{
          width: '160px',
          background: '#1a1a1a',
          borderRadius: '12px',
          padding: '12px',
        }}>
          <h3 style={{ marginBottom: '12px', fontSize: '14px' }}>컨트롤</h3>

          <button
            onClick={() => setPlacedItems([])}
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
            🗑️ 전체 초기화
          </button>

          <button
            onClick={() => setPlacedItems(prev => prev.slice(0, -1))}
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
            ↩ 되돌리기
          </button>

          <button
            onClick={reset}
            style={{
              width: '100%',
              padding: '10px',
              background: '#e74c3c',
              color: 'white',
              border: 'none',
              borderRadius: '8px',
              cursor: 'pointer',
            }}
          >
            🔄 처음부터
          </button>
        </div>
      </div>
    </div>
  )
}