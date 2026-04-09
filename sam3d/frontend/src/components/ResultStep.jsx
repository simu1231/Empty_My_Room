import { useStore } from '../store/useStore'

export default function ResultStep() {
  const { inpaintedUrl, originalUrl, reset, setStep } = useStore()

  return (
    <div style={{ padding: '20px', textAlign: 'center' }}>
      <h2 style={{ color: 'white', marginBottom: '20px' }}>결과</h2>

      <div style={{ display: 'flex', gap: '20px', marginTop: '20px' }}>
        <div style={{ flex: 1 }}>
          <h3 style={{ color: 'white', marginBottom: '10px' }}>원본</h3>
          {originalUrl && (
            <img src={originalUrl} style={{ width: '100%', borderRadius: '12px' }} />
          )}
        </div>

        <div style={{ flex: 1 }}>
          <h3 style={{ color: 'white', marginBottom: '10px' }}>가구 제거 후</h3>
          {inpaintedUrl
            ? <img src={inpaintedUrl} style={{ width: '100%', borderRadius: '12px' }} />
            : <div style={{ background: '#1a1a1a', borderRadius: '12px', padding: '60px', color: '#666' }}>AI 처리 결과가 여기 표시돼요</div>
          }
        </div>
      </div>

      <div style={{ marginTop: '30px', display: 'flex', gap: '12px', justifyContent: 'center' }}>
        {inpaintedUrl && (
          <a href={inpaintedUrl} download="result.jpg" style={{ padding: '12px 30px', background: '#27ae60', color: 'white', borderRadius: '8px', textDecoration: 'none', fontSize: '16px' }}>
            💾 결과 다운로드
          </a>
        )}

        {inpaintedUrl && (
          <button onClick={() => setStep('relocate')} style={{ padding: '12px 30px', background: '#3498db', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '16px' }}>
            가구 재배치
          </button>
        )}

        <button onClick={reset} style={{ padding: '12px 30px', background: '#333', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '16px' }}>
          🔄 처음부터 다시
        </button>
      </div>
    </div>
  )
}