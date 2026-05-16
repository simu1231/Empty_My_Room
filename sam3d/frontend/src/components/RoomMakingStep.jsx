import { useRef, useEffect, useState, useCallback } from 'react'
import { useStore } from '../store/useStore'
import toast from 'react-hot-toast'

// SAM3D가 추출한 색/크기로 깔끔한 박스 방 미리보기
// 첫 번째 결과와 동일한 코드 - SAM3D mesh + DoubleSide + 외부 카메라 + 색 입힘
function MeshPreview({ meshData }) {
  const mountRef = useRef(null)

  useEffect(() => {
    if (!mountRef.current || !meshData) return
    const THREE = window.THREE
    const el = mountRef.current
    const W = el.clientWidth, H = el.clientHeight

    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setSize(W, H)
    renderer.setPixelRatio(window.devicePixelRatio)
    el.appendChild(renderer.domElement)

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x13131a)
    scene.add(new THREE.AmbientLight(0xffffff, 0.55))
    const key = new THREE.DirectionalLight(0xffffff, 0.7)
    key.position.set(2, 6, 4)
    scene.add(key)
    const fill = new THREE.DirectionalLight(0xffffff, 0.2)
    fill.position.set(-3, 2, -2)
    scene.add(fill)

    const camera = new THREE.PerspectiveCamera(55, W / H, 0.01, 200)

    const geo = new THREE.BufferGeometry()
    const numVerts = meshData.vertices.length
    const verts = new Float32Array(numVerts * 3)
    for (let i = 0; i < numVerts; i++) {
      verts[i*3]     =  meshData.vertices[i][0]
      verts[i*3 + 1] = -meshData.vertices[i][1]  // Y 반전
      verts[i*3 + 2] =  meshData.vertices[i][2]
    }
    const numFaces = meshData.faces.length
    const faces = new Uint32Array(numFaces * 3)
    for (let i = 0; i < numFaces; i++) {
      faces[i*3] = meshData.faces[i][0]; faces[i*3+1] = meshData.faces[i][1]; faces[i*3+2] = meshData.faces[i][2]
    }
    const numColors = meshData.colors.length
    const cols = new Float32Array(numColors * 3)
    for (let i = 0; i < numColors; i++) {
      cols[i*3] = meshData.colors[i][0]; cols[i*3+1] = meshData.colors[i][1]; cols[i*3+2] = meshData.colors[i][2]
    }

    geo.setAttribute('position', new THREE.BufferAttribute(verts, 3))
    geo.setIndex(new THREE.BufferAttribute(faces, 1))
    geo.setAttribute('color', new THREE.BufferAttribute(cols, 3))
    geo.computeVertexNormals()
    geo.computeBoundingBox()

    const center = new THREE.Vector3(); geo.boundingBox.getCenter(center)
    const size   = new THREE.Vector3(); geo.boundingBox.getSize(size)
    const maxDim = Math.max(size.x, size.y, size.z)

    const mat  = new THREE.MeshStandardMaterial({ vertexColors: true, side: THREE.DoubleSide, roughness: 0.85, metalness: 0 })
    const mesh = new THREE.Mesh(geo, mat)
    mesh.position.sub(center)
    scene.add(mesh)

    camera.position.set(0, maxDim * 0.8, maxDim * 1.2)
    camera.lookAt(0, 0, 0)

    let dragging = false, px = 0, py = 0
    const lookAt = new THREE.Vector3(0, 0, 0)
    renderer.domElement.addEventListener('mousedown', e => { dragging = true; px = e.clientX; py = e.clientY })
    window.addEventListener('mousemove', e => {
      if (!dragging) return
      const sph = new THREE.Spherical().setFromVector3(camera.position.clone().sub(lookAt))
      sph.theta -= (e.clientX - px) * 0.007
      sph.phi    = Math.max(0.3, Math.min(1.3, sph.phi + (e.clientY - py) * 0.005))
      camera.position.copy(new THREE.Vector3().setFromSpherical(sph).add(lookAt))
      camera.lookAt(lookAt)
      px = e.clientX; py = e.clientY
    })
    window.addEventListener('mouseup', () => { dragging = false })
    renderer.domElement.addEventListener('wheel', e => {
      e.preventDefault()
      camera.position.multiplyScalar(1 + e.deltaY * 0.001)
      camera.lookAt(lookAt)
    }, { passive: false })

    let id
    const loop = () => { id = requestAnimationFrame(loop); renderer.render(scene, camera) }
    loop()

    return () => {
      cancelAnimationFrame(id)
      renderer.dispose()
      if (el.contains(renderer.domElement)) el.removeChild(renderer.domElement)
    }
  }, [meshData])

  return <div ref={mountRef} style={{ width: '100%', height: '100%' }} />
}

export default function RoomMakingStep() {
  const { emptyRoomUrl, emptyRoomFile, setStep, setRoomSize, setRoomMesh, reset } = useStore()

  const canvasRef   = useRef(null)
  const debounceRef = useRef(null)

  const [points, setPoints]       = useState([])
  const [maskB64, setMaskB64]     = useState(null)
  const [maskLoading, setML]      = useState(false)
  const [generating, setGen]      = useState(false)
  const [meshData, setMeshData]   = useState(null)
  const [showPreview, setPreview] = useState(false)

  const [width, setW]  = useState(4.5)
  const [depth, setD]  = useState(3.5)
  const [height, setH] = useState(2.5)

  const redraw = useCallback(() => {
    if (!emptyRoomUrl || !canvasRef.current) return
    const c = canvasRef.current, ctx = c.getContext('2d')
    const img = new Image(); img.src = emptyRoomUrl
    img.onload = async () => {
      c.width = img.width; c.height = img.height
      ctx.drawImage(img, 0, 0)
      if (maskB64) {
        const mi = new Image(); mi.src = `data:image/png;base64,${maskB64}`
        await new Promise(res => { mi.onload = res })
        const off = document.createElement('canvas')
        off.width = c.width; off.height = c.height
        const oc = off.getContext('2d')
        oc.drawImage(mi, 0, 0, c.width, c.height)
        const d = oc.getImageData(0, 0, c.width, c.height)
        for (let i = 0; i < d.data.length; i += 4) {
          if (d.data[i] > 128) { d.data[i] = 99; d.data[i+1] = 179; d.data[i+2] = 237; d.data[i+3] = 110 }
          else d.data[i+3] = 0
        }
        oc.putImageData(d, 0, 0); ctx.drawImage(off, 0, 0)
      }
      points.forEach(pt => {
        ctx.beginPath(); ctx.arc(pt.x, pt.y, 8, 0, Math.PI * 2)
        ctx.fillStyle = 'rgba(99,179,237,0.9)'; ctx.fill()
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.stroke()
      })
    }
  }, [emptyRoomUrl, maskB64, points])

  useEffect(() => { redraw() }, [redraw])

  const fetchMask = useCallback(async (pts) => {
    if (!pts.length || !emptyRoomFile) return
    setML(true)
    try {
      const form = new FormData()
      form.append('image', emptyRoomFile)
      form.append('points', JSON.stringify(pts.map(p => [p.x, p.y])))
      const res = await fetch('http://127.0.0.1:8001/api/segment/mask', { method: 'POST', body: form })
      const data = await res.json()
      if (data.success) setMaskB64(data.mask_b64)
    } catch (_) {}
    setML(false)
  }, [emptyRoomFile])

  const handleClick = e => {
    const c = canvasRef.current, rect = c.getBoundingClientRect()
    const x = Math.round((e.clientX - rect.left) * c.width / rect.width)
    const y = Math.round((e.clientY - rect.top)  * c.height / rect.height)
    const np = [...points, { x, y }]
    setPoints(np)
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => fetchMask(np), 300)
  }

  const undo = () => {
    const np = points.slice(0, -1)
    setPoints(np)
    if (!np.length) setMaskB64(null)
    else { clearTimeout(debounceRef.current); debounceRef.current = setTimeout(() => fetchMask(np), 300) }
  }

  const clear = () => { setPoints([]); setMaskB64(null) }

  const canGenerate = points.length > 0

  const handleGenerate = async () => {
    if (!canGenerate) return
    setGen(true)
    try {
      const form = new FormData()
      form.append('image', emptyRoomFile)
      form.append('room_points', JSON.stringify(points.map(p => [p.x, p.y])))
      const res = await fetch('http://127.0.0.1:8001/api/room/generate3d', { method: 'POST', body: form })
      const data = await res.json()
      if (!data.success) throw new Error(data.error)
      setMeshData(data.mesh)
      setPreview(true)
    } catch (e) { toast.error('생성 실패: ' + e.message) }
    setGen(false)
  }

  const handleStart = () => {
    setRoomSize({ width: parseFloat(width), depth: parseFloat(depth), height: parseFloat(height) })
    setRoomMesh(meshData)
    setStep('interior3d')
  }

  const hasMask = points.length > 0
  const hasMesh = !!meshData

  return (
    <div style={{ display: 'flex', gap: 20, alignItems: 'flex-start', fontFamily: "'Inter', 'Segoe UI', sans-serif", color: '#f0f0f0' }}>

      {/* 캔버스 영역 */}
      <div style={{ flex: 1, position: 'relative', borderRadius: 12, overflow: 'hidden', border: '1px solid #222', background: '#111' }}>

        {/* 선택 배지 */}
        <div style={{ position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)', zIndex: 10, display: 'flex', alignItems: 'center', gap: 8, background: 'rgba(10,10,18,0.85)', backdropFilter: 'blur(12px)', borderRadius: 24, padding: '6px 16px', border: '1px solid #2a2a3e' }}>
          <div style={{ width: 7, height: 7, borderRadius: '50%', background: '#63b3ed' }} />
          <span style={{ fontSize: 13, fontWeight: 600, color: '#63b3ed' }}>방 내부 선택</span>
          {points.length > 0 && <span style={{ fontSize: 11, color: '#666' }}>{points.length}pt</span>}
          {maskLoading && <span style={{ fontSize: 12, color: '#63b3ed' }}>분석중...</span>}
          {points.length > 0 && <>
            <div style={{ width: 1, height: 12, background: '#333' }} />
            <button onClick={undo} style={{ background: 'none', border: 'none', color: '#888', cursor: 'pointer', fontSize: 12, padding: '1px 5px' }}>↩</button>
            <button onClick={clear} style={{ background: 'none', border: 'none', color: '#555', cursor: 'pointer', fontSize: 12, padding: '1px 5px' }}>✕</button>
          </>}
        </div>

        {/* 캔버스 */}
        <canvas
          ref={canvasRef}
          onClick={handleClick}
          style={{ width: '100%', height: 'auto', display: 'block', cursor: 'crosshair', maxHeight: 560, objectFit: 'contain' }}
        />

        {/* 힌트 */}
        {!hasMask && (
          <div style={{ position: 'absolute', bottom: 14, left: '50%', transform: 'translateX(-50%)', background: 'rgba(0,0,0,0.6)', color: '#aaa', padding: '7px 16px', borderRadius: 20, fontSize: 13, pointerEvents: 'none', whiteSpace: 'nowrap' }}>
            방 내부(벽·바닥)를 클릭해 선택하세요
          </div>
        )}
      </div>

      {/* 오른쪽 패널 */}
      <div style={{ width: 220, flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 12 }}>

        {/* 선택 현황 */}
        <div style={{ background: '#111', border: '1px solid #222', borderRadius: 12, padding: '16px 14px' }}>
          <div style={{ fontSize: 11, color: '#444', letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 10 }}>선택 현황</div>
          <div style={{ padding: '9px 12px', borderRadius: 8, background: '#0a0a0a', border: `1px solid ${hasMask ? '#63b3ed40' : '#1a1a28'}`, display: 'flex', alignItems: 'center', gap: 8 }}>
            <div style={{ width: 7, height: 7, borderRadius: '50%', background: hasMask ? '#63b3ed' : '#2a2a3e' }} />
            <span style={{ fontSize: 13, color: hasMask ? '#e0e0f0' : '#444' }}>방 내부</span>
            {hasMask && <span style={{ fontSize: 11, color: '#63b3ed', marginLeft: 'auto' }}>{points.length}pt ✓</span>}
          </div>
        </div>

        {/* 방 크기 */}
        <div style={{ background: '#111', border: '1px solid #222', borderRadius: 12, padding: '16px 14px' }}>
          <div style={{ fontSize: 11, color: '#444', letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 12 }}>방 크기 (m)</div>
          {[['가로', width, setW], ['세로', depth, setD], ['높이', height, setH]].map(([label, val, set]) => (
            <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <label style={{ fontSize: 12, color: '#555', width: 30 }}>{label}</label>
              <input type="number" value={val} onChange={e => set(e.target.value)} min="1" max="20" step="0.5"
                style={{ flex: 1, padding: '6px 8px', background: '#0a0a0a', border: '1px solid #222', borderRadius: 7, color: '#c0c0d0', fontSize: 13, outline: 'none' }} />
              <span style={{ fontSize: 11, color: '#333' }}>m</span>
            </div>
          ))}
        </div>

        {/* 생성 버튼 */}
        <button onClick={handleGenerate} disabled={!canGenerate || generating} style={{
          width: '100%', padding: '13px', borderRadius: 10, border: 'none',
          background: canGenerate ? 'linear-gradient(135deg, #5b6eff, #9b5cff)' : '#1a1a1a',
          color: canGenerate ? '#fff' : '#333', fontSize: 14, fontWeight: 600,
          cursor: canGenerate ? 'pointer' : 'not-allowed',
          opacity: generating ? 0.6 : 1,
        }}>
          {generating ? '3D 생성 중...' : '✨ SAM3D 생성'}
        </button>
        {generating && (
          <p style={{ fontSize: 11, color: '#555', textAlign: 'center', lineHeight: 1.6, margin: 0 }}>
            SAM3D가 방 구조를<br />3D로 변환 중 (약 30-60초)
          </p>
        )}
        {!canGenerate && !generating && (
          <p style={{ fontSize: 11, color: '#333', textAlign: 'center', margin: 0 }}>
            방 내부를 먼저 클릭해주세요
          </p>
        )}
      </div>

      {/* 3D 미리보기 모달 */}
      {showPreview && meshData && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(4,4,10,0.92)', zIndex: 100, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}>
          <div style={{ width: '100%', maxWidth: 900, background: '#0d0d1a', borderRadius: 18, overflow: 'hidden', border: '1px solid #1e1e3a', boxShadow: '0 32px 80px rgba(0,0,0,0.6)' }}>

            <div style={{ padding: '18px 24px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid #1a1a2e' }}>
              <div>
                <div style={{ fontSize: 16, fontWeight: 700 }}>SAM3D 방 미리보기</div>
                <div style={{ fontSize: 12, color: '#444', marginTop: 2 }}>드래그로 회전 · 스크롤로 줌</div>
              </div>
              <div style={{ display: 'flex', gap: 10 }}>
                <button onClick={() => setPreview(false)} style={{ padding: '9px 18px', background: '#1a1a2e', border: '1px solid #2a2a3e', color: '#888', borderRadius: 9, cursor: 'pointer', fontSize: 13 }}>
                  다시 선택
                </button>
                <button onClick={handleStart} style={{ padding: '9px 24px', background: 'linear-gradient(135deg, #3b82f6, #6366f1)', border: 'none', color: '#fff', borderRadius: 9, cursor: 'pointer', fontSize: 13, fontWeight: 600 }}>
                  이 방으로 시작 →
                </button>
              </div>
            </div>

            <div style={{ height: 420 }}>
              <MeshPreview meshData={meshData} />
            </div>

            <div style={{ padding: '14px 24px', borderTop: '1px solid #1a1a2e', display: 'flex', alignItems: 'center', gap: 20 }}>
              <span style={{ fontSize: 12, color: '#444' }}>방 크기 조정</span>
              {[['가로', width, setW], ['세로', depth, setD], ['높이', height, setH]].map(([label, val, set]) => (
                <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <label style={{ fontSize: 12, color: '#555' }}>{label}</label>
                  <input type="number" value={val} onChange={e => set(e.target.value)} min="1" max="20" step="0.5"
                    style={{ width: 56, padding: '5px 7px', background: '#0f0f1e', border: '1px solid #1e1e2e', borderRadius: 6, color: '#c0c0d0', fontSize: 13, outline: 'none' }} />
                  <span style={{ fontSize: 11, color: '#333' }}>m</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
