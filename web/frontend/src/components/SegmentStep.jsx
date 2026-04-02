import { useRef, useEffect } from 'react'
import { useStore } from '../store/useStore'
import toast from 'react-hot-toast'

export default function SegmentStep() {
  const {
    originalFile, originalUrl,
    clickPoints, addPoint, clearPoints,
    setStep, setLoading, setInpainted
  } = useStore()

  const canvasRef = useRef(null)

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
        ctx.font = 'bold 12px Arial'
        ctx.fillText(i + 1, pt.x - 4, pt.y + 4)
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
    addPoint({ x, y })
    toast.success(`포인트 ${clickPoints.length + 1} 추가!`)
  }

  const handleRemove = async () => {
    if (clickPoints.length === 0) {
      toast.error('가구를 먼저 클릭해서 선택하세요!')
      return
    }

    setLoading(true, 'SAM2가 가구 영역 분석중...')

    try {
      // 1단계: SAM2로 마스크 생성
      const form1 = new FormData()
      form1.append('image', originalFile)
      form1.append('points', JSON.stringify(clickPoints.map(p => [p.x, p.y])))

      const res1 = await fetch('http://127.0.0.1:8000/api/segment/mask', {
        method: 'POST',
        body: form1,
      })
      const data1 = await res1.json()
      if (!data1.success) throw new Error('마스크 생성 실패')

      toast.success('마스크 생성 완료! LaMa 인페인팅 중...')
      setLoading(true, 'LaMa가 가구 제거중...')

      // 마스크 파일 변환
      const maskBlob = await fetch(
        `data:image/png;base64,${data1.mask_b64}`
      ).then(r => r.blob())
      const maskFile = new File([maskBlob], 'mask.png', { type: 'image/png' })

      // 리사이즈된 이미지 파일 변환
      const resizedBlob = await fetch(
        `data:image/png;base64,${data1.resized_image_b64}`
      ).then(r => r.blob())
      const resizedFile = new File([resizedBlob], 'resized.png', { type: 'image/png' })

      // 2단계: LaMa + SD로 가구 제거
      const form2 = new FormData()
      form2.append('image', resizedFile)
      form2.append('mask', maskFile)

      const res2 = await fetch('http://127.0.0.1:8000/api/inpaint/remove', {
        method: 'POST',
        body: form2,
      })
      const data2 = await res2.json()
      if (!data2.success) throw new Error('인페인팅 실패')

      // 결과 이미지 URL 생성
      const resultBlob = await fetch(
        `data:image/jpeg;base64,${data2.result_b64}`
      ).then(r => r.blob())
      const resultUrl = URL.createObjectURL(resultBlob)

      setInpainted(resultUrl)
      setStep('result')
      toast.success('가구 제거 완료! 🎉')

    } catch (e) {
      toast.error(`오류 발생: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ padding: '20px' }}>
      <h2>제거할 가구를 클릭하세요</h2>
      <p style={{ color: '#aaa' }}>
        이미지 위에서 제거하고 싶은 가구를 클릭하세요. 여러 개 클릭 가능해요.
      </p>

      <div style={{ display: 'flex', gap: '20px', marginTop: '20px' }}>
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

          <div style={{ flex: 1, overflowY: 'auto', marginBottom: '10px' }}>
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