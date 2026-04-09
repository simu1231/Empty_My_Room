import { useRef, useEffect, useState } from 'react'
import { useStore } from '../store/useStore'
import toast from 'react-hot-toast'

function MeshViewer({ data }) {
  const mountRef = useRef(null)

  useEffect(() => {
    if (!data || !mountRef.current) return

    const THREE = window.THREE
    const el = mountRef.current
    const width = el.clientWidth
    const height = 400

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x1a1a1a)

    const camera = new THREE.PerspectiveCamera(45, width / height, 0.01, 1000)

    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setSize(width, height)
    el.appendChild(renderer.domElement)

    scene.add(new THREE.AmbientLight(0xffffff, 0.8))
    const dir1 = new THREE.DirectionalLight(0xffffff, 1.0)
    dir1.position.set(5, 10, 5)
    scene.add(dir1)

    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(data.vertices.flat()), 3))
    geometry.setAttribute('color', new THREE.BufferAttribute(new Float32Array(data.colors.flat()), 3))
    geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(data.faces.flat()), 1))
    geometry.computeVertexNormals()
    geometry.center()

    geometry.computeBoundingBox()
    const size = new THREE.Vector3()
    geometry.boundingBox.getSize(size)
    const maxDim = Math.max(size.x, size.y, size.z)
    camera.position.set(0, maxDim * 0.5, maxDim * 1.5)
    camera.lookAt(0, 0, 0)

    const material = new THREE.MeshStandardMaterial({ vertexColors: true, side: THREE.DoubleSide })
    const group = new THREE.Group()
    const mesh = new THREE.Mesh(geometry, material)
    mesh.rotation.x = -Math.PI / 2
    group.add(mesh)
    scene.add(group)

    let isDragging = false, prevX = 0, prevY = 0
    const onDown = (e) => { isDragging = true; prevX = e.clientX; prevY = e.clientY }
    const onMove = (e) => {
      if (!isDragging) return
      group.rotation.y += (e.clientX - prevX) * 0.01
      group.rotation.x += (e.clientY - prevY) * 0.01
      prevX = e.clientX; prevY = e.clientY
    }
    const onUp = () => { isDragging = false }
    const onWheel = (e) => {
      e.preventDefault()
      camera.position.z = Math.max(0.1, Math.min(50, camera.position.z + e.deltaY * 0.05))
    }

    renderer.domElement.addEventListener('mousedown', onDown)
    renderer.domElement.addEventListener('mousemove', onMove)
    renderer.domElement.addEventListener('mouseup', onUp)
    renderer.domElement.addEventListener('mouseleave', onUp)
    renderer.domElement.addEventListener('wheel', onWheel, { passive: false })

    let animId
    const animate = () => { animId = requestAnimationFrame(animate); renderer.render(scene, camera) }
    animate()

    return () => {
      cancelAnimationFrame(animId)
      renderer.dispose()
      if (el.contains(renderer.domElement)) el.removeChild(renderer.domElement)
    }
  }, [data])

  return <div ref={mountRef} style={{ width: '100%', height: '400px', borderRadius: '8px', overflow: 'hidden', cursor: 'grab' }} />
}

export default function Interior3DStep() {
  const { furnitureList, roomSize, reset } = useStore()
  const [meshData, setMeshData] = useState(null)
  const [generating, setGenerating] = useState(false)
  const [selectedFurniture, setSelectedFurniture] = useState(null)

  const generate3DMesh = async (furniture) => {
    setGenerating(true)
    setSelectedFurniture(furniture)
    toast.success('SAM3D로 3D 메쉬 생성 중... (30초~1분)')
    try {
      const imgBlob = await fetch(`data:image/png;base64,${furniture.b64}`).then(r => r.blob())
      const imgFile = new File([imgBlob], 'furniture.png', { type: 'image/png' })

      const form = new FormData()
      form.append('image', imgFile)

      const res = await fetch('http://127.0.0.1:8001/api/sam3d/mesh', { method: 'POST', body: form })
      const data = await res.json()
      if (!data.success) throw new Error(data.error || '3D 생성 실패')

      setMeshData(data.mesh)
      toast.success(`3D 메쉬 생성 완료! 버텍스: ${data.vertices}개 🎉`)
    } catch (e) {
      toast.error(`3D 생성 실패: ${e.message}`)
    } finally {
      setGenerating(false)
    }
  }

  return (
    <div style={{ padding: '20px' }}>
      <h2>3D 인테리어 배치</h2>
      <p style={{ color: '#aaa' }}>방 크기: {roomSize.width}m × {roomSize.depth}m × {roomSize.height}m</p>

      <div style={{ display: 'flex', gap: '20px', marginTop: '20px' }}>
        {/* 가구 목록 */}
        <div style={{ width: '180px', background: '#1a1a1a', borderRadius: '12px', padding: '12px', overflowY: 'auto', maxHeight: '600px' }}>
          <h3 style={{ marginBottom: '12px', fontSize: '14px' }}>추출된 가구</h3>
          {furnitureList.length === 0 ? (
            <p style={{ color: '#666', fontSize: '13px' }}>가구 없음</p>
          ) : (
            furnitureList.map((f, i) => (
              <div key={i} style={{
                background: selectedFurniture?.id === f.id ? '#1a3a5c' : '#2a2a2a',
                borderRadius: '8px', padding: '8px', marginBottom: '8px',
                border: selectedFurniture?.id === f.id ? '2px solid #3498db' : '2px solid transparent'
              }}>
                <img src={`data:image/png;base64,${f.b64}`} style={{ width: '100%', borderRadius: '4px' }} />
                <p style={{ fontSize: '11px', color: '#aaa', marginTop: '4px', textAlign: 'center' }}>{f.name || `가구 ${i + 1}`}</p>
                <button
                  onClick={() => generate3DMesh(f)}
                  disabled={generating}
                  style={{
                    width: '100%', padding: '6px', marginTop: '4px',
                    background: generating ? '#555' : '#3498db',
                    color: 'white', border: 'none', borderRadius: '6px',
                    cursor: generating ? 'not-allowed' : 'pointer', fontSize: '11px'
                  }}
                >
                  {generating && selectedFurniture?.id === f.id ? '생성 중...' : '🔷 3D 변환'}
                </button>
              </div>
            ))
          )}
        </div>

        {/* 3D 뷰어 */}
        <div style={{ flex: 1 }}>
          {meshData ? (
            <>
              <p style={{ color: '#aaa', fontSize: '13px', marginBottom: '8px' }}>마우스로 회전 · 스크롤로 확대/축소</p>
              <MeshViewer data={meshData} />
            </>
          ) : (
            <div style={{
              width: '100%', height: '400px', background: '#1a1a1a',
              borderRadius: '12px', display: 'flex', alignItems: 'center',
              justifyContent: 'center', color: '#666', flexDirection: 'column', gap: '12px'
            }}>
              <div style={{ fontSize: '48px' }}>🔷</div>
              <p>왼쪽에서 가구를 선택하고 3D 변환을 눌러보세요</p>
            </div>
          )}
        </div>

        {/* 컨트롤 */}
        <div style={{ width: '160px', background: '#1a1a1a', borderRadius: '12px', padding: '12px' }}>
          <h3 style={{ marginBottom: '12px', fontSize: '14px' }}>컨트롤</h3>
          <button
            onClick={reset}
            style={{ width: '100%', padding: '10px', background: '#e74c3c', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer' }}
          >
            🔄 처음부터
          </button>
        </div>
      </div>
    </div>
  )
}
