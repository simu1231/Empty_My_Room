import { useState } from 'react'
import { useStore } from '../store/useStore'

export default function RoomSetupStep() {
  const { setStep, setRoomSize, furnitureList } = useStore()
  const [width, setWidth] = useState(5)
  const [depth, setDepth] = useState(4)
  const [height, setHeight] = useState(2.5)

  const handleNext = () => {
    setRoomSize({ width: parseFloat(width), depth: parseFloat(depth), height: parseFloat(height) })
    setStep('interior3d')
  }

  return (
    <div style={{ padding: '40px', maxWidth: '500px', margin: '0 auto' }}>
      <h2>방 크기를 설정하세요</h2>
      <p style={{ color: '#aaa', marginBottom: '30px' }}>추출된 가구 {furnitureList.length}개를 3D 방에 배치합니다</p>

      {[
        { label: '가로 (m)', value: width, set: setWidth },
        { label: '세로 (m)', value: depth, set: setDepth },
        { label: '높이 (m)', value: height, set: setHeight },
      ].map(({ label, value, set }) => (
        <div key={label} style={{ marginBottom: '20px' }}>
          <label style={{ display: 'block', marginBottom: '8px', color: '#aaa' }}>{label}</label>
          <input
            type="number"
            value={value}
            onChange={e => set(e.target.value)}
            min="1" max="20" step="0.5"
            style={{ width: '100%', padding: '12px', background: '#1a1a1a', border: '2px solid #333', borderRadius: '8px', color: 'white', fontSize: '16px' }}
          />
        </div>
      ))}

      <button
        onClick={handleNext}
        style={{ width: '100%', padding: '14px', background: '#3498db', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '16px', fontWeight: 'bold', marginTop: '10px' }}
      >
        3D 배치 시작 →
      </button>
    </div>
  )
}
