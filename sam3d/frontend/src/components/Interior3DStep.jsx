import { useRef, useEffect, useState } from 'react'
import { useStore } from '../store/useStore'
import toast from 'react-hot-toast'

function RoomViewer({ roomSize, furnitureMeshes }) {
  const mountRef = useRef(null)
  const sceneRef = useRef(null)
  const rendererRef = useRef(null)
  const cameraRef = useRef(null)
  const furnitureGroupsRef = useRef({})

  useEffect(() => {
    if (!mountRef.current) return
    const THREE = window.THREE
    const el = mountRef.current
    const width = el.clientWidth
    const height = 500

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x2a2a2a)
    sceneRef.current = scene

    const camera = new THREE.PerspectiveCamera(45, width / height, 0.01, 1000)
    camera.position.set(roomSize.width, roomSize.height * 2, roomSize.depth * 2)
    camera.lookAt(0, 0, 0)
    cameraRef.current = camera

    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setSize(width, height)
    el.appendChild(renderer.domElement)
    rendererRef.current = renderer

    // 조명
    scene.add(new THREE.AmbientLight(0xffffff, 0.7))
    const dir1 = new THREE.DirectionalLight(0xffffff, 0.8)
    dir1.position.set(5, 10, 5)
    scene.add(dir1)

    // 방 박스 (와이어프레임)
    const roomGeo = new THREE.BoxGeometry(roomSize.width, roomSize.height, roomSize.depth)
    const roomMat = new THREE.MeshBasicMaterial({ color: 0x444444, wireframe: true })
    const roomMesh = new THREE.Mesh(roomGeo, roomMat)
    scene.add(roomMesh)

    // 바닥
    const floorGeo = new THREE.PlaneGeometry(roomSize.width, roomSize.depth)
    const floorMat = new THREE.MeshStandardMaterial({ color: 0x8B6914, side: THREE.DoubleSide })
    const floor = new THREE.Mesh(floorGeo, floorMat)
    floor.rotation.x = -Math.PI / 2
    floor.position.y = -roomSize.height / 2
    scene.add(floor)

    // 드래그 회전
    let isDragging = false, prevX = 0, prevY = 0
    const pivot = new THREE.Group()
    scene.add(pivot)

    const onDown = (e) => { isDragging = true; prevX = e.clientX; prevY = e.clientY }
    const onMove = (e) => {
      if (!isDragging) return
      pivot.rotation.y += (e.clientX - prevX) * 0.01
      pivot.rotation.x += (e.clientY - prevY) * 0.005
      prevX = e.clientX; prevY = e.clientY
    }
    const onUp = () => { isDragging = false }
    const onWheel = (e) => {
      e.preventDefault()
      camera.position.multiplyScalar(1 + e.deltaY * 0.001)
    }

    renderer.domElement.addEventListener('mousedown', onDown)
    renderer.domElement.addEventListener('mousemove', onMove)
    renderer.domElement.addEventListener('mouseup', onUp)
    renderer.domElement.addEventListener('mouseleave', onUp)
    renderer.domElement.addEventListener('wheel', onWheel, { passive: false })

    let animId
    const animate = () => {
      animId = requestAnimationFrame(animate)
      renderer.render(scene, camera)
    }
    animate()

    return () => {
      cancelAnimationFrame(animId)
      renderer.dispose()
      if (el.contains(renderer.domElement)) el.removeChild(renderer.domElement)
    }
  }, [roomSize])

  // 가구 메쉬 추가
  useEffect(() => {
    if (!sceneRef.current) return
    const THREE = window.THREE

    Object.entries(furnitureMeshes).forEach(([id, data]) => {
      if (furnitureGroupsRef.current[id]) return // 이미 추가됨

      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(data.vertices.flat()), 3))
      geometry.setAttribute('color', new THREE.BufferAttribute(new Float32Array(data.colors.flat()), 3))
      geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(data.faces.flat()), 1))
      geometry.computeVertexNormals()
      geometry.center()

      // 크기 정규화
      geometry.computeBoundingBox()
      const size = new THREE.Vector3()
      geometry.boundingBox.getSize(size)
      const maxDim = Math.max(size.x, size.y, size.z)
      const scale = 1.0 / maxDim

      const material = new THREE.MeshStandardMaterial({ vertexColors: true, side: THREE.DoubleSide })
      const mesh = new THREE.Mesh(geometry, material)
      mesh.rotation.x = -Math.PI / 2
      mesh.scale.set(scale, scale, scale)

      const group = new THREE.Group()
      group.add(mesh)
      sceneRef.current.add(group)
      furnitureGroupsRef.current[id] = group
    })
  }, [furnitureMeshes])

  return <div ref={mountRef} style={{ width: '100%', height: '500px', borderRadius: '8px', overflow: 'hidden', cursor: 'grab' }} />
}

function MeshPreview({ data }) {
  const mountRef = useRef(null)

  useEffect(() => {
    if (!data || !mountRef.current) return
    const THREE = window.THREE
    const el = mountRef.current
    const width = el.clientWidth
    const height = 300

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x1a1a1a)
    const camera = new THREE.PerspectiveCamera(45, width / height, 0.01, 1000)
    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setSize(width, height)
    el.appendChild(renderer.domElement)

    scene.add(new THREE.AmbientLight(0xffffff, 0.8))
    const dir = new THREE.DirectionalLight(0xffffff, 1.0)
    dir.position.set(5, 10, 5)
    scene.add(dir)

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

  return <div ref={mountRef} style={{ width: '100%', height: '300px', borderRadius: '8px', overflow: 'hidden', cursor: 'grab' }} />
}

export default function Interior3DStep() {
  const { furnitureList, roomSize, reset } = useStore()
  const [furnitureMeshes, setFurnitureMeshes] = useState({})
  const [generating, setGenerating] = useState(false)
  const [selectedFurniture, setSelectedFurniture] = useState(null)
  const [previewMesh, setPreviewMesh] = useState(null)
  const [activeTab, setActiveTab] = useState('preview') // 'preview' or 'room'

  const generate3DMesh = async (furniture) => {
    setGenerating(true)
    setSelectedFurniture(furniture)
    toast.success('SAM3D로 3D 메쉬 생성 중... (20~30분 소요)')
    try {
      const imgBlob = await fetch(`data:image/png;base64,${furniture.b64}`).then(r => r.blob())
      const imgFile = new File([imgBlob], 'furniture.png', { type: 'image/png' })

      const form = new FormData()
      form.append('image', imgFile)

      const res = await fetch('http://127.0.0.1:8001/api/sam3d/mesh', { method: 'POST', body: form })
      const data = await res.json()
      if (!data.success) throw new Error(data.error || '3D 생성 실패')

      setPreviewMesh(data.mesh)
      setFurnitureMeshes(prev => ({ ...prev, [furniture.id]: data.mesh }))
      setActiveTab('preview')
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

      {/* 탭 */}
      <div style={{ display: 'flex', gap: '8px', margin: '12px 0' }}>
        <button onClick={() => setActiveTab('preview')}
          style={{ padding: '8px 20px', borderRadius: '8px', border: 'none', cursor: 'pointer', background: activeTab === 'preview' ? '#3498db' : '#333', color: 'white' }}>
          🔷 가구 3D 미리보기
        </button>
        <button onClick={() => setActiveTab('room')}
          style={{ padding: '8px 20px', borderRadius: '8px', border: 'none', cursor: 'pointer', background: activeTab === 'room' ? '#27ae60' : '#333', color: 'white' }}>
          🏠 3D 방 배치
        </button>
      </div>

      <div style={{ display: 'flex', gap: '20px' }}>
        {/* 가구 목록 */}
        <div style={{ width: '180px', background: '#1a1a1a', borderRadius: '12px', padding: '12px', overflowY: 'auto', maxHeight: '600px' }}>
          <h3 style={{ marginBottom: '12px', fontSize: '14px' }}>추출된 가구</h3>
          {furnitureList.map((f, i) => (
            <div key={i} style={{
              background: selectedFurniture?.id === f.id ? '#1a3a5c' : '#2a2a2a',
              borderRadius: '8px', padding: '8px', marginBottom: '8px',
              border: selectedFurniture?.id === f.id ? '2px solid #3498db' : '2px solid transparent'
            }}>
              <img src={`data:image/png;base64,${f.b64}`} style={{ width: '100%', borderRadius: '4px' }} />
              <p style={{ fontSize: '11px', color: '#aaa', marginTop: '4px', textAlign: 'center' }}>{f.name || `가구 ${i + 1}`}</p>
              {furnitureMeshes[f.id] && <p style={{ fontSize: '10px', color: '#27ae60', textAlign: 'center' }}>✅ 3D 완료</p>}
              <button
                onClick={() => generate3DMesh(f)}
                disabled={generating}
                style={{
                  width: '100%', padding: '6px', marginTop: '4px',
                  background: generating && selectedFurniture?.id === f.id ? '#555' : furnitureMeshes[f.id] ? '#27ae60' : '#3498db',
                  color: 'white', border: 'none', borderRadius: '6px',
                  cursor: generating ? 'not-allowed' : 'pointer', fontSize: '11px'
                }}
              >
                {generating && selectedFurniture?.id === f.id ? '생성 중...' : furnitureMeshes[f.id] ? '🔄 재생성' : '🔷 3D 변환'}
              </button>
            </div>
          ))}
        </div>

        {/* 뷰어 */}
        <div style={{ flex: 1 }}>
          {activeTab === 'preview' && (
            previewMesh ? <MeshPreview data={previewMesh} /> : (
              <div style={{ width: '100%', height: '300px', background: '#1a1a1a', borderRadius: '12px', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#666', flexDirection: 'column', gap: '12px' }}>
                <div style={{ fontSize: '48px' }}>🔷</div>
                <p>왼쪽에서 가구를 선택하고 3D 변환을 눌러보세요</p>
              </div>
            )
          )}
          {activeTab === 'room' && (
            <RoomViewer roomSize={roomSize} furnitureMeshes={furnitureMeshes} />
          )}
        </div>

        {/* 컨트롤 */}
        <div style={{ width: '160px', background: '#1a1a1a', borderRadius: '12px', padding: '12px' }}>
          <h3 style={{ marginBottom: '12px', fontSize: '14px' }}>컨트롤</h3>
          <p style={{ fontSize: '12px', color: '#aaa', marginBottom: '8px' }}>
            변환된 가구: {Object.keys(furnitureMeshes).length}개
          </p>
          <button onClick={reset} style={{ width: '100%', padding: '10px', background: '#e74c3c', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer' }}>
            🔄 처음부터
          </button>
        </div>
      </div>
    </div>
  )
}