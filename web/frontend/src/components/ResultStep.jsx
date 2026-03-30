import { useStore } from '../store/useStore'

export default function ResultStep() {
  const { inpaintedUrl, originalUrl, reset } = useStore()

  return (
    <div style={{ padding: '20px', textAlign: 'center' }}>
      <h2>✅ 결과</h2>

      <div style={{ display: 'flex', gap: '20px', marginTop: '20px' }}>
        {/* 원본 */}
        <div style={{ flex: 1 }}>
          <h3>원본</h3>
          {originalUrl && (
            <img
              src={originalUrl}
              style={{ width: '100%', borderRadius: '12px' }}
            />
          )}
        </div>

        {/* 결과 */}
        <div style={{ flex: 1 }}>
          <h3>가구 제거 후</h3>
          {inpaintedUrl ? (
            <img
              src={inpaintedUrl}
              style={{ width: '100%', borderRadius: '12px' }}
            />
          ) : (
            <div style={{
              background: '#1a1a1a',
              borderRadius: '12px',
              padding: '60px',
              color: '#666'
            }}>
              AI 처리 결과가 여기 표시돼요
            </div>
          )}
        </div>
      </div>

      <button
        onClick={reset}
        style={{
          marginTop: '30px',
          padding: '12px 30px',
          background: '#333',
          color: 'white',
          border: 'none',
          borderRadius: '8px',
          cursor: 'pointer',
          fontSize: '16px',
        }}
      >
        🔄 처음부터 다시
      </button>
    </div>
  )
}