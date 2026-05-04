import { useStore } from './store/useStore'
import UploadStep from './components/UploadStep'
import SegmentStep from './components/SegmentStep'
import RoomMakingStep from './components/RoomMakingStep'
import Interior3DStep from './components/Interior3DStep'
import { Toaster } from 'react-hot-toast'
import './App.css'

function App() {
  const { step, loading, loadingMsg } = useStore()
  return (
    <div className="app">
      <Toaster position="top-center" />
      {loading && (
        <div className="loading-overlay">
          <div className="loading-box">
            <div className="spinner" />
            <p>{loadingMsg || 'AI 처리중...'}</p>
          </div>
        </div>
      )}
      <header className="header">
        <h1>🛋️ 3D Interior Designer</h1>
        <p>AI로 가구를 3D로 변환하고 공간을 디자인하세요</p>
      </header>
      <div className="steps-bar">
        {[
          { key: 'upload',      label: '사진 업로드' },
          { key: 'segment',     label: '가구 선택' },
          { key: 'roommaking',  label: '방 만들기' },
          { key: 'interior3d',  label: '3D 배치' },
        ].map((s, i) => (
          <div key={s.key} className={`step-item ${step === s.key ? 'active' : ''}`}>
            <span className="step-num">{i + 1}</span>
            <span className="step-label">{s.label}</span>
          </div>
        ))}
      </div>
      <main className="main">
        {step === 'upload'     && <UploadStep />}
        {step === 'segment'    && <SegmentStep />}
        {step === 'roommaking' && <RoomMakingStep />}
        {step === 'interior3d' && <Interior3DStep />}
      </main>
    </div>
  )
}

export default App