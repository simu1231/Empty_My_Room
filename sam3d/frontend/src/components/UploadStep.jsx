import { useCallback, useEffect, useState } from 'react'
import { useDropzone } from 'react-dropzone'
import { useStore } from '../store/useStore'

const DB_NAME = 'EmptyMyRoomDesigns', DB_VER = 1, STORE = 'designs'
function openDB() {
  return new Promise((res, rej) => {
    const req = indexedDB.open(DB_NAME, DB_VER)
    req.onupgradeneeded = e => e.target.result.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true })
    req.onsuccess = e => res(e.target.result)
    req.onerror = e => rej(e.target.error)
  })
}
async function dbLoadDesigns() {
  const db = await openDB()
  return new Promise((res, rej) => {
    const req = db.transaction(STORE, 'readonly').objectStore(STORE).getAll()
    req.onsuccess = () => res(req.result)
    req.onerror = e => rej(e.target.error)
  })
}
async function dbDeleteDesign(id) {
  const db = await openDB()
  return new Promise((res, rej) => {
    const req = db.transaction(STORE, 'readwrite').objectStore(STORE).delete(id)
    req.onsuccess = () => res()
    req.onerror = e => rej(e.target.error)
  })
}

export default function UploadStep() {
  const { setOriginalImage, setStep, setSavedDesignToLoad, setFurnitureList } = useStore()
  const [savedDesigns, setSavedDesigns] = useState([])

  useEffect(() => {
    dbLoadDesigns().then(setSavedDesigns).catch(() => {})
  }, [])

  const loadDesign = (design) => {
    if (design.furnitureList) setFurnitureList(design.furnitureList)
    setSavedDesignToLoad({
      furnitureMeshes: design.furnitureMeshes,
      placedMeshes: design.placedMeshes,
    })
    setStep('interior3d')
  }

  const deleteDesign = async (e, id) => {
    e.stopPropagation()
    await dbDeleteDesign(id)
    setSavedDesigns(prev => prev.filter(d => d.id !== id))
  }

  const onDrop = useCallback((files) => {
    if (!files.length) return
    setOriginalImage(files[0])
    setStep('segment')
  }, [setOriginalImage, setStep])

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: { 'image/*': ['.jpg', '.jpeg', '.png'] },
    multiple: false,
  })

  return (
    <div style={{ textAlign: 'center', padding: '40px', maxWidth: '800px', margin: '0 auto' }}>
      <h2>방 사진을 업로드하세요</h2>
      <p>가구를 제거하고 싶은 방 사진을 올려주세요</p>

      <div
        {...getRootProps()}
        style={{
          border: '3px dashed #666',
          borderRadius: '16px',
          padding: '60px 40px',
          cursor: 'pointer',
          marginTop: '24px',
          background: isDragActive ? '#1a1a2e' : '#111',
          transition: 'all 0.2s',
        }}
      >
        <input {...getInputProps()} />
        <div style={{ fontSize: '60px' }}>🖼️</div>
        <p style={{ fontSize: '18px', marginTop: '16px' }}>
          {isDragActive ? '여기에 놓으세요!' : '클릭하거나 사진을 드래그하세요'}
        </p>
        <p style={{ color: '#888', fontSize: '14px' }}>JPG, PNG 지원</p>
      </div>

      {/* 저장된 디자인 불러오기 */}
      {savedDesigns.length > 0 && (
        <div style={{ marginTop: '36px', textAlign: 'left' }}>
          <h3 style={{ fontSize: '16px', color: '#ccc', marginBottom: '14px' }}>
            💾 저장된 디자인 불러오기
          </h3>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: '12px' }}>
            {savedDesigns.slice().reverse().map(d => (
              <div key={d.id} onClick={() => loadDesign(d)}
                style={{
                  position: 'relative', borderRadius: '12px', overflow: 'hidden',
                  cursor: 'pointer', border: '2px solid #333',
                  transition: 'border-color 0.2s',
                }}
                onMouseEnter={e => e.currentTarget.style.borderColor = '#27ae60'}
                onMouseLeave={e => e.currentTarget.style.borderColor = '#333'}
              >
                <img src={d.thumbnail} alt="저장된 디자인"
                  style={{ width: '100%', aspectRatio: '4/3', objectFit: 'cover', display: 'block' }} />
                <div style={{
                  padding: '6px 8px', background: '#1a1a1a',
                  fontSize: '11px', color: '#aaa',
                }}>
                  {new Date(d.timestamp).toLocaleDateString('ko-KR', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                </div>
                <button onClick={e => deleteDesign(e, d.id)}
                  style={{
                    position: 'absolute', top: '6px', right: '6px',
                    background: 'rgba(231,76,60,0.85)', border: 'none', borderRadius: '50%',
                    width: '22px', height: '22px', cursor: 'pointer',
                    color: 'white', fontSize: '12px', lineHeight: '22px',
                  }}>✕</button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}