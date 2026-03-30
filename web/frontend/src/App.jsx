import { useStore } from './store/useStore'
import UploadStep from './components/UploadStep'
import SegmentStep from './components/SegmentStep'
import ResultStep from './components/ResultStep'
import { Toaster } from 'react-hot-toast'
import './App.css'

function App() {
  const { step, loading, loadingMsg } = useStore()

  return (
    <div className="app">
      <Toaster position="top-center" />

      {/* 로딩 오버레이 */}
      {loading && (
        <div className="loading-overlay">
          <div className="loading-box">
            <div className="spinner" />
            <p>{loadingMsg || 'AI 처리중...'}</p>
          </div>
        </div>
      )}

      {/* 헤더 */}
      <header className="header">
        <h1>🛋️ Furniture Remover</h1>
        <p>AI로 가구를 제거하고 공간을 새롭게</p>
      </header>

      {/* 단계 표시 */}
      <div className="steps-bar">
        {['upload', 'segment', 'result'].map((s, i) => (
          <div key={s} className={`step-item ${step === s ? 'active' : ''}`}>
            <span className="step-num">{i + 1}</span>
            <span className="step-label">
              {s === 'upload' ? '사진 업로드' : s === 'segment' ? '가구 선택' : '결과'}
            </span>
          </div>
        ))}
      </div>

      {/* 메인 콘텐츠 */}
      <main className="main">
        {step === 'upload' && <UploadStep />}
        {step === 'segment' && <SegmentStep />}
        {step === 'result' && <ResultStep />}
      </main>
    </div>
  )
}

export default App