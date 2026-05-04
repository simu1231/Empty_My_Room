import { useRef, useEffect, useState } from 'react'
import { useStore } from '../store/useStore'
import toast from 'react-hot-toast'

function Room3DViewer({ roomSize, wallColor, floorColor }) {
  const mountRef = useRef(null)

  useEffect(() => {
    if (!mountRef.current) return
    const THREE = window.THREE
    const el = mountRef.current
    const width = el.clientWidth
    const height = 400

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x2a2a2a)

    const camera = new THREE.PerspectiveCamera(45, width / height, 0.01, 1000)
    camera.position.set(roomSize.width * 0.8, roomSize.height * 1.2, roomSize.depth * 1.5)
    camera.lookAt(0, 0, 0)

    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setSize(width, height)
    el.appendChild(renderer.domElement)

    scene.add(new THREE.AmbientLight(0xffffff, 0.8))
    const dir = new THREE.DirectionalLight(0xffffff, 0.6)
    dir.position.set(5, 10, 5)
    scene.add(dir)

    const w = roomSize.width
    const h = roomSize.height
    const d = roomSize.depth

    const wallMat = new THREE.MeshStandardMaterial({
      color: new THREE.Color(wallColor[0], wallColor[1], wallColor[2]),
      side: THREE.BackSide,
    })
    const floorMat = new THREE.MeshStandardMaterial({
      color: new THREE.Color(floorColor[0], floorColor[1], floorColor[2]),
    })

    // 바닥
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(w, d), floorMat)
    floor.rotation.x = -Math.PI / 2
    floor.position.y = -h / 2
    scene.add(floor)

    // 뒷벽
    const backWall = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshStandardMaterial({ color: new THREE.Color(wallColor[0], wallColor[1], wallColor[2]) }))
    backWall.position.z = -d / 2
    scene.add(backWall)

    // 왼쪽 벽
    const leftWall = new THREE.Mesh(new THREE.PlaneGeometry(d, h), new THREE.MeshStandardMaterial({ color: new THREE.Color(wallColor[0], wallColor[1], wallColor[2]) }))
    leftWall.rotation.y = Math.PI / 2
    leftWall.position.x = -w / 2
    scene.add(leftWall)

    // 오른쪽 벽
    const rightWall = new THREE.Mesh(new THREE.PlaneGeometry(d, h), new THREE.MeshStandardMaterial({ color: new THREE.Color(wallColor[0], wallColor[1], wallColor[2]) }))
    rightWall.rotation.y = -Math.PI / 2
    rightWall.position.x = w / 2
    scene.add(rightWall)

    // 천장
    const ceiling = new THREE.Mesh(new THREE.PlaneGeometry(w, d), new THREE.MeshStandardMaterial({ color: 0xffffff }))
    ceiling.rotation.x = Math.PI / 2
    ceiling.position.y = h / 2
    scene.add(ceiling)

    // 카메라 회전
    let isDragging = false, prevX = 0, prevY = 0
    const onDown = (e) => { isDragging = true; prevX = e.clientX; prevY = e.clientY }
    const onMove = (e) => {
      if (!isDragging) return
      const dx = e.clientX - prevX
      const dy = e.clientY - prevY
      const spherical = new THREE.Spherical().setFromVector3(camera.position)
      spherical.theta -= dx * 0.01
      spherical.phi -= dy * 0.005
      spherical.phi = Math.max(0.1, Math.min(Math.PI / 2, spherical.phi))
      camera.position.setFromSpherical(spherical)
      camera.lookAt(0, 0, 0)
      prevX = e.clientX; prevY = e.clientY
    }
    const onUp = () => { isDragging = false }
    const onWheel = (e) => {
      e.preventDefault()
      camera.position.multiplyScalar(1 + e.deltaY * 0.001)
      camera.lookAt(0, 0, 0)
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
  }, [roomSize, wallColor, floorColor])

  return <div ref={mountRef} style={{ width: '100%', height: '400px', borderRadius: '12px', overflow: 'hidden', cursor: 'grab' }} />
}

export default function RoomMakingStep() {
  const { emptyRoomUrl, emptyRoomFile, setStep, setRoomSize, setRoomColors, setRoomTextures,reset } = useStore()
  const [width, setWidth] = useState(5)
  const [depth, setDepth] = useState(4)
  const [height, setHeight] = useState(2.5)
  const [wallColor, setWallColor] = useState([0.9, 0.9, 0.9])
  const [floorColor, setFloorColor] = useState([0.6, 0.4, 0.2])
  const [colorsExtracted, setColorsExtracted] = useState(false)

  useEffect(() => {
    if (emptyRoomFile) extractColors()
  }, [emptyRoomFile])

  const extractColors = async () => {
    try {
      const form = new FormData()
      form.append('image', emptyRoomFile)
      const res = await fetch('http://127.0.0.1:8001/api/room/extract-colors', {
        method: 'POST',
        body: form,
      })
      const data = await res.json()
      if (data.success) {
        setWallColor(data.wall_color)
        setFloorColor(data.floor_color)
        setRoomColors({ wall: data.wall_color, floor: data.floor_color })
        setRoomTextures({ wall: data.wall_texture, floor: data.floor_texture })  // 추가
        setColorsExtracted(true)
        toast.success('방 색상 추출 완료! 🎨')
      }
    } catch (e) {
      toast.error('색상 추출 실패')
    }
  }

  const handleNext = () => {
    setRoomSize({ width: parseFloat(width), depth: parseFloat(depth), height: parseFloat(height) })
    setStep('interior3d')
  }

  const toHex = (rgb) => {
    return '#' + rgb.map(v => Math.round(v * 255).toString(16).padStart(2, '0')).join('')
  }

  return (
    <div style={{ padding: '20px' }}>
      <h2>방 만들기</h2>
      <p style={{ color: '#aaa', marginBottom: '20px' }}>빈방 사진을 확인하고 3D 방을 생성하세요</p>

      <div style={{ display: 'flex', gap: '30px' }}>
        {/* 왼쪽: 2D 빈방 */}
        <div style={{ flex: 1 }}>
          <h3 style={{ marginBottom: '12px', fontSize: '15px', color: '#aaa' }}>📷 2D 빈방</h3>
          {emptyRoomUrl ? (
            <img src={emptyRoomUrl} style={{ width: '100%', borderRadius: '12px', border: '2px solid #333' }} />
          ) : (
            <div style={{ width: '100%', height: '400px', background: '#1a1a1a', borderRadius: '12px', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#666' }}>
              빈방 이미지 없음
            </div>
          )}
        </div>

        {/* 오른쪽: 3D 방 */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <div>
            <h3 style={{ marginBottom: '8px', fontSize: '15px', color: '#aaa' }}>
              🏠 3D 방
              {colorsExtracted && (
                <span style={{ marginLeft: '8px', fontSize: '12px', color: '#27ae60' }}>
                  ● 색상 추출 완료
                </span>
              )}
            </h3>
            <Room3DViewer
              roomSize={{ width: parseFloat(width), depth: parseFloat(depth), height: parseFloat(height) }}
              wallColor={wallColor}
              floorColor={floorColor}
            />
          </div>

          {/* 색상 표시 */}
          {colorsExtracted && (
            <div style={{ display: 'flex', gap: '12px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <div style={{ width: '24px', height: '24px', borderRadius: '4px', background: toHex(wallColor), border: '1px solid #555' }} />
                <span style={{ fontSize: '13px', color: '#aaa' }}>벽 색상</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <div style={{ width: '24px', height: '24px', borderRadius: '4px', background: toHex(floorColor), border: '1px solid #555' }} />
                <span style={{ fontSize: '13px', color: '#aaa' }}>바닥 색상</span>
              </div>
            </div>
          )}

          {/* 방 크기 설정 */}
          <div style={{ background: '#1a1a1a', borderRadius: '12px', padding: '16px' }}>
            <h3 style={{ marginBottom: '12px', fontSize: '15px' }}>방 크기 설정</h3>
            {[
              { label: '가로 (m)', value: width, set: setWidth },
              { label: '세로 (m)', value: depth, set: setDepth },
              { label: '높이 (m)', value: height, set: setHeight },
            ].map(({ label, value, set }) => (
              <div key={label} style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '10px' }}>
                <label style={{ color: '#aaa', width: '80px', fontSize: '14px' }}>{label}</label>
                <input
                  type="number"
                  value={value}
                  onChange={e => set(e.target.value)}
                  min="1" max="20" step="0.5"
                  style={{ flex: 1, padding: '8px', background: '#2a2a2a', border: '2px solid #333', borderRadius: '8px', color: 'white', fontSize: '14px' }}
                />
              </div>
            ))}
          </div>

          <div style={{ display: 'flex', gap: '8px' }}>
            <button onClick={reset} style={{ flex: 1, padding: '12px', background: '#e74c3c', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer' }}>
              🔄 처음부터
            </button>
            <button onClick={handleNext} style={{ flex: 2, padding: '12px', background: '#3498db', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '15px', fontWeight: 'bold' }}>
              3D 배치 시작 →
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
