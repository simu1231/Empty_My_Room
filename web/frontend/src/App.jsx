import { useStore } from './store/useStore'
import UploadStep from './components/UploadStep'
import SegmentStep from './components/SegmentStep'
import ResultStep from './components/ResultStep'
import RelocateStep from './components/RelocateStep'
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
        {[
          { key: 'upload',   label: '사진 업로드' },
          { key: 'segment',  label: '가구 선택' },
          { key: 'result',   label: '결과' },
          { key: 'relocate', label: '재배치' },
        ].map((s, i) => (
          <div key={s.key} className={`step-item ${step === s.key ? 'active' : ''}`}>
            <span className="step-num">{i + 1}</span>
            <span className="step-label">{s.label}</span>
          </div>
        ))}
      </div>

      {/* 메인 콘텐츠 */}
      <main className="main">
        {step === 'upload'   && <UploadStep />}
        {step === 'segment'  && <SegmentStep />}
        {step === 'result'   && <ResultStep />}
        {step === 'relocate' && <RelocateStep />}
      </main>
    </div>
  )
}

export default App