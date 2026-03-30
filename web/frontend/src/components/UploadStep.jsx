import { useCallback } from 'react'
import { useDropzone } from 'react-dropzone'
import { useStore } from '../store/useStore'

export default function UploadStep() {
  const { setOriginalImage, setStep } = useStore()

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
    <div style={{ textAlign: 'center', padding: '40px' }}>
      <h2>방 사진을 업로드하세요</h2>
      <p>가구를 제거하고 싶은 방 사진을 올려주세요</p>

      <div
        {...getRootProps()}
        style={{
          border: '3px dashed #666',
          borderRadius: '16px',
          padding: '80px 40px',
          cursor: 'pointer',
          marginTop: '30px',
          background: isDragActive ? '#1a1a2e' : '#111',
          transition: 'all 0.2s',
        }}
      >
        <input {...getInputProps()} />
        <div style={{ fontSize: '60px' }}>🖼️</div>
        <p style={{ fontSize: '18px', marginTop: '16px' }}>
          {isDragActive ? '여기에 놓으세요!' : '클릭하거나 사진을 드래그하세요'}
        </p>
        <p style={{ color: '#888', fontSize: '14px' }}>
          JPG, PNG 지원
        </p>
      </div>
    </div>
  )
}