import { useRef, useEffect, useState, useCallback } from 'react'
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
    geometry.setAttribute('color',    new THREE.BufferAttribute(new Float32Array(data.colors.flat()), 3))
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
 
    let isDragging = false
    let prevX = 0, prevY = 0
 
    const onDown = (e) => { isDragging = true; prevX = e.clientX; prevY = e.clientY }
    const onMove = (e) => {
      if (!isDragging) return
      group.rotation.y += (e.clientX - prevX) * 0.01
      group.rotation.x += (e.clientY - prevY) * 0.01
      prevX = e.clientX
      prevY = e.clientY
    }
    const onUp = () => { isDragging = false }
    const onWheel = (e) => {
      e.preventDefault()
      camera.position.z = Math.max(0.1, Math.min(50, camera.position.z + e.deltaY * 0.01))
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
 
  return (
    <div ref={mountRef} style={{ width: '100%', height: '400px', borderRadius: '8px', overflow: 'hidden', cursor: 'grab' }} />
  )
}
 
function GaussianViewer({ data }) {
  const mountRef = useRef(null)
 
  useEffect(() => {
    if (!data || !mountRef.current) return
 
    const THREE = window.THREE
    const width  = mountRef.current.clientWidth
    const height = 400
 
    const scene    = new THREE.Scene()
    scene.background = new THREE.Color(0x111111)
 
    const camera = new THREE.PerspectiveCamera(60, width / height, 0.01, 100)
    camera.position.set(0, 0, 5)
 
    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setSize(width, height)
    mountRef.current.appendChild(renderer.domElement)
 
    const canvas2d = document.createElement('canvas')
    canvas2d.width = 64
    canvas2d.height = 64
    const ctx2d = canvas2d.getContext('2d')
    const grad = ctx2d.createRadialGradient(32, 32, 0, 32, 32, 32)
    grad.addColorStop(0, 'rgba(255,255,255,1)')
    grad.addColorStop(1, 'rgba(255,255,255,0)')
    ctx2d.fillStyle = grad
    ctx2d.fillRect(0, 0, 64, 64)
 
    const spriteTex = new THREE.CanvasTexture(canvas2d)
    const group = new THREE.Group()
 
    data.means.forEach((pt, i) => {
      const c = data.colors[i]
      const mat = new THREE.SpriteMaterial({
        map: spriteTex,
        color: new THREE.Color(c[0], c[1], c[2]),
        transparent: true,
        opacity: 1.0,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      })
      const sprite = new THREE.Sprite(mat)
      sprite.position.set(pt[0], -pt[1], -pt[2])
      sprite.scale.set(0.3, 0.3, 0.3)
      group.add(sprite)
    })
 
    scene.add(group)
 
    const box = new THREE.Box3().setFromObject(group)
    const center = box.getCenter(new THREE.Vector3())
    group.position.sub(center)
 
    let isDragging = false
    let prevX = 0, prevY = 0
    let autoRotate = true
 
    const onMouseDown = (e) => { isDragging = true; autoRotate = false; prevX = e.clientX; prevY = e.clientY }
    const onMouseUp   = ()  => { isDragging = false }
    const onMouseMove = (e) => {
      if (!isDragging) return
      group.rotation.y += (e.clientX - prevX) * 0.01
      group.rotation.x += (e.clientY - prevY) * 0.01
      prevX = e.clientX; prevY = e.clientY
    }
    const onWheel = (e) => {
      e.preventDefault()
      camera.position.z += e.deltaY * 0.001
      camera.position.z = Math.max(0.5, Math.min(5, camera.position.z))
    }
 
    const el = renderer.domElement
    el.addEventListener('mousedown', onMouseDown)
    window.addEventListener('mouseup', onMouseUp)
    window.addEventListener('mousemove', onMouseMove)
    el.addEventListener('wheel', onWheel, { passive: false })
 
    let animId
    const animate = () => {
      animId = requestAnimationFrame(animate)
      if (autoRotate) group.rotation.y += 0.005
      renderer.render(scene, camera)
    }
    animate()
 
    return () => {
      cancelAnimationFrame(animId)
      el.removeEventListener('mousedown', onMouseDown)
      window.removeEventListener('mouseup', onMouseUp)
      window.removeEventListener('mousemove', onMouseMove)
      el.removeEventListener('wheel', onWheel)
      renderer.dispose()
      if (mountRef.current && renderer.domElement.parentNode === mountRef.current) {
        mountRef.current.removeChild(renderer.domElement)
      }
    }
  }, [data])
 
  return (
    <div ref={mountRef} style={{ width: '100%', height: '400px', borderRadius: '8px', overflow: 'hidden', cursor: 'grab' }} />
  )
}
 
function PointCloudViewer({ data }) {
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
 
    const positions = []
    const colors = []
    data.points.forEach((pt, i) => {
      positions.push(pt[0], -pt[1], -pt[2])
      const c = data.colors[i]
      colors.push(c[0], c[1], c[2])
    })
 
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3))
    geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3))
    geometry.center()
 
    geometry.computeBoundingBox()
    const size = new THREE.Vector3()
    geometry.boundingBox.getSize(size)
    const maxDim = Math.max(size.x, size.y, size.z)
    camera.position.set(0, maxDim * 0.5, maxDim * 1.5)
    camera.lookAt(0, 0, 0)
 
    const material = new THREE.PointsMaterial({ size: 0.3, vertexColors: true })
    const group = new THREE.Group()
    group.add(new THREE.Points(geometry, material))
    scene.add(group)
 
    let isDragging = false, prevX = 0, prevY = 0
    const onDown  = (e) => { isDragging = true; prevX = e.clientX; prevY = e.clientY }
    const onMove  = (e) => {
      if (!isDragging) return
      group.rotation.y += (e.clientX - prevX) * 0.01
      group.rotation.x += (e.clientY - prevY) * 0.01
      prevX = e.clientX; prevY = e.clientY
    }
    const onUp    = () => { isDragging = false }
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
 
export default function RelocateStep() {
  const { originalFile, inpaintedUrl, clickPoints, maskB64, reset } = useStore()
 
  const canvasRef = useRef(null)
  const [furnitureList, setFurnitureList] = useState([])
  const [placedItems, setPlacedItems] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [loading, setLoading] = useState(false)
  const [bgImage, setBgImage] = useState(null)
  const imgCache = useRef({})
  const dragState = useRef(null)
  const [views3D, setViews3D] = useState(null)
  const [generating3D, setGenerating3D] = useState(false)
 
  const extracted = useRef(false)
  useEffect(() => {
    if (!originalFile || !clickPoints.length || extracted.current) return
    extracted.current = true
    extractFurniture()
  }, [])
 
  useEffect(() => {
    if (!inpaintedUrl) return
    const img = new Image()
    img.src = inpaintedUrl
    img.onload = () => setBgImage(img)
  }, [inpaintedUrl])
 
  const getImg = (b64) => {
    if (imgCache.current[b64]) return imgCache.current[b64]
    const img = new Image()
    img.src = `data:image/png;base64,${b64}`
    imgCache.current[b64] = img
    return img
  }
 
  const redraw = useCallback(() => {
    if (!bgImage) return
    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')
    canvas.width = bgImage.width
    canvas.height = bgImage.height
    ctx.drawImage(bgImage, 0, 0)
 
    placedItems.forEach(item => {
      const img = getImg(item.b64)
      const cx = item.x + item.width / 2
      const cy = item.y + item.height / 2
 
      ctx.save()
      ctx.translate(cx, cy)
      ctx.rotate((item.rotation || 0) * Math.PI / 180)
      if (item.flipped) ctx.scale(-1, 1)
      ctx.drawImage(img, -item.width / 2, -item.height / 2, item.width, item.height)
      ctx.restore()
 
      if (selectedId === item.id) {
        ctx.save()
        ctx.translate(cx, cy)
        ctx.rotate((item.rotation || 0) * Math.PI / 180)
        ctx.strokeStyle = '#00ff00'
        ctx.lineWidth = 3
        ctx.strokeRect(-item.width / 2, -item.height / 2, item.width, item.height)
 
        const corners = [
          [-item.width / 2, -item.height / 2],
          [item.width / 2, -item.height / 2],
          [item.width / 2, item.height / 2],
          [-item.width / 2, item.height / 2],
        ]
        corners.forEach(([hx, hy]) => {
          ctx.fillStyle = '#00ff00'
          ctx.fillRect(hx - 7, hy - 7, 14, 14)
        })
        ctx.restore()
 
        const rotHandleX = cx + Math.sin((item.rotation || 0) * Math.PI / 180) * (-item.height / 2 - 30)
        const rotHandleY = cy - Math.cos((item.rotation || 0) * Math.PI / 180) * (item.height / 2 + 30)
        ctx.beginPath()
        ctx.arc(rotHandleX, rotHandleY, 10, 0, Math.PI * 2)
        ctx.fillStyle = '#3498db'
        ctx.fill()
        ctx.strokeStyle = 'white'
        ctx.lineWidth = 2
        ctx.stroke()
        ctx.fillStyle = 'white'
        ctx.font = 'bold 12px Arial'
        ctx.textAlign = 'center'
        ctx.textBaseline = 'middle'
        ctx.fillText('↻', rotHandleX, rotHandleY)
 
        const flipX = cx
        const flipY = cy + Math.cos((item.rotation || 0) * Math.PI / 180) * (item.height / 2 + 25)
        ctx.beginPath()
        ctx.roundRect(flipX - 20, flipY - 10, 40, 20, 6)
        ctx.fillStyle = '#e67e22'
        ctx.fill()
        ctx.fillStyle = 'white'
        ctx.font = 'bold 16px Arial'
        ctx.textAlign = 'center'
        ctx.textBaseline = 'middle'
        ctx.fillText('↔', flipX, flipY)
      }
    })
  }, [bgImage, placedItems, selectedId])
 
  useEffect(() => { redraw() }, [redraw])
 
  const getCanvasPos = (e) => {
    const canvas = canvasRef.current
    const rect = canvas.getBoundingClientRect()
    return {
      x: (e.clientX - rect.left) * (canvas.width / rect.width),
      y: (e.clientY - rect.top) * (canvas.height / rect.height),
    }
  }
 
  const getRotatedLocal = (item, pos) => {
    const cx = item.x + item.width / 2
    const cy = item.y + item.height / 2
    const angle = -(item.rotation || 0) * Math.PI / 180
    const dx = pos.x - cx
    const dy = pos.y - cy
    return {
      lx: dx * Math.cos(angle) - dy * Math.sin(angle),
      ly: dx * Math.sin(angle) + dy * Math.cos(angle),
    }
  }
 
  const handleMouseDown = (e) => {
    const pos = getCanvasPos(e)
 
    if (selectedId !== null) {
      const item = placedItems.find(i => i.id === selectedId)
      if (item) {
        const cx = item.x + item.width / 2
        const cy = item.y + item.height / 2
 
        const rotHandleX = cx + Math.sin((item.rotation || 0) * Math.PI / 180) * (-item.height / 2 - 30)
        const rotHandleY = cy - Math.cos((item.rotation || 0) * Math.PI / 180) * (item.height / 2 + 30)
        if (Math.hypot(pos.x - rotHandleX, pos.y - rotHandleY) < 14) {
          dragState.current = { type: 'rotate', cx, cy, itemId: item.id }
          return
        }
 
        const flipY = cy + Math.cos((item.rotation || 0) * Math.PI / 180) * (item.height / 2 + 25)
        if (Math.abs(pos.x - cx) < 30 && Math.abs(pos.y - flipY) < 14) {
          setPlacedItems(prev => prev.map(i =>
            i.id === selectedId ? { ...i, flipped: !i.flipped } : i
          ))
          return
        }
 
        const { lx, ly } = getRotatedLocal(item, pos)
        const hw = item.width / 2
        const hh = item.height / 2
        const corners = [
          { lx: -hw, ly: -hh, corner: 'nw' },
          { lx: hw, ly: -hh, corner: 'ne' },
          { lx: hw, ly: hh, corner: 'se' },
          { lx: -hw, ly: hh, corner: 'sw' },
        ]
        for (let h of corners) {
          if (Math.hypot(lx - h.lx, ly - h.ly) < 12) {
            dragState.current = { type: 'resize', corner: h.corner, startPos: pos, origItem: { ...item } }
            return
          }
        }
      }
    }
 
    for (let i = placedItems.length - 1; i >= 0; i--) {
      const item = placedItems[i]
      const { lx, ly } = getRotatedLocal(item, pos)
      if (Math.abs(lx) <= item.width / 2 && Math.abs(ly) <= item.height / 2) {
        setSelectedId(item.id)
        dragState.current = { type: 'move', startX: pos.x - item.x, startY: pos.y - item.y, itemId: item.id }
        return
      }
    }
    setSelectedId(null)
  }
 
  const handleMouseMove = (e) => {
    if (!dragState.current) return
    const pos = getCanvasPos(e)
    const ds = dragState.current
 
    if (ds.type === 'move') {
      setPlacedItems(prev => prev.map(item =>
        item.id === ds.itemId ? { ...item, x: pos.x - ds.startX, y: pos.y - ds.startY } : item
      ))
    } else if (ds.type === 'rotate') {
      const angle = Math.atan2(pos.x - ds.cx, -(pos.y - ds.cy)) * 180 / Math.PI
      const snapped = Math.round(angle / 30) * 30
      setPlacedItems(prev => prev.map(item =>
        item.id === selectedId ? { ...item, rotation: snapped } : item
      ))
    } else if (ds.type === 'resize') {
      const orig = ds.origItem
      const dx = pos.x - ds.startPos.x
      const dy = pos.y - ds.startPos.y
      const angle = (orig.rotation || 0) * Math.PI / 180
      const ldx = dx * Math.cos(-angle) - dy * Math.sin(-angle)
      const ldy = dx * Math.sin(-angle) + dy * Math.cos(-angle)
 
      let { x, y, width, height } = orig
      if (ds.corner === 'se') { width = Math.max(50, orig.width + ldx); height = Math.max(50, orig.height + ldy) }
      else if (ds.corner === 'sw') { x = orig.x + ldx; width = Math.max(50, orig.width - ldx); height = Math.max(50, orig.height + ldy) }
      else if (ds.corner === 'ne') { y = orig.y + ldy; width = Math.max(50, orig.width + ldx); height = Math.max(50, orig.height - ldy) }
      else if (ds.corner === 'nw') { x = orig.x + ldx; y = orig.y + ldy; width = Math.max(50, orig.width - ldx); height = Math.max(50, orig.height - ldy) }
 
      setPlacedItems(prev => prev.map(item =>
        item.id === selectedId ? { ...item, x, y, width, height } : item
      ))
    }
  }
 
  const handleMouseUp = () => { dragState.current = null }
 
  const handleDrop = (e) => {
    e.preventDefault()
    const furnitureId = e.dataTransfer.getData('furnitureId')
    const furniture = furnitureList.find(f => String(f.id) === furnitureId)
    if (!furniture) return
    const pos = getCanvasPos(e)
    const newItem = {
      ...furniture,
      id: Date.now(),
      x: pos.x - 100,
      y: pos.y - 100,
      width: 200,
      height: 200,
      rotation: 0,
      flipped: false,
    }
    setPlacedItems(prev => [...prev, newItem])
    setSelectedId(newItem.id)
    toast.success('가구 배치! 모서리로 크기 조절, 위 파란 버튼으로 회전, 아래 버튼으로 반전')
  }
 
  const extractFurniture = async () => {
    setLoading(true)
    try {
      const form = new FormData()
      form.append('image', originalFile)
      form.append('points', JSON.stringify(clickPoints.map(p => [p.x, p.y])))
      form.append('labels', JSON.stringify(clickPoints.map(p => p.label || '기타')))
      const res = await fetch('http://127.0.0.1:8000/api/extract/furniture', { method: 'POST', body: form })
      const data = await res.json()
      if (!data.success) throw new Error('추출 실패')
      setFurnitureList(data.furniture)
      toast.success(`가구 ${data.furniture.length}개 추출 완료!`)
    } catch (e) {
      toast.error(`가구 추출 실패: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }
 
  const generate3DViews = async (e, furniture) => {
    e.stopPropagation()
    setGenerating3D(true)
    toast.success('TripoSR 3D 메쉬 생성 중...')
    try {
      const imgBlob = await fetch(`data:image/png;base64,${furniture.b64}`).then(r => r.blob())
      const imgFile = new File([imgBlob], 'furniture.png', { type: 'image/png' })
 
      const form = new FormData()
      form.append('image', imgFile)
 
      const res = await fetch('http://127.0.0.1:8000/api/triposr/mesh', { method: 'POST', body: form })
      const data = await res.json()
      if (!data.success) throw new Error('TripoSR 생성 실패')
 
      setViews3D({ mesh: data.mesh, vertices: data.vertices, faces: data.faces })
      toast.success(`TripoSR 완료! 버텍스 ${data.vertices}개 🎉`)
    } catch (e) {
      toast.error(`3D 생성 실패: ${e.message}`)
    } finally {
      setGenerating3D(false)
    }
  }
 
  return (
    <>
      {views3D && (
        <div
          style={{
            position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.9)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            zIndex: 999,
          }}
          onMouseDown={e => e.stopPropagation()}
          onMouseMove={e => e.stopPropagation()}
          onMouseUp={e => e.stopPropagation()}
        >
          <div style={{ background: '#1a1a1a', borderRadius: '16px', padding: '24px', maxWidth: '800px', width: '90%' }}>
            <h3 style={{ marginBottom: '8px' }}>🔷 TripoSR 3D 메쉬 뷰어</h3>
            <p style={{ color: '#aaa', fontSize: '13px', marginBottom: '12px' }}>
              버텍스: {views3D.vertices?.toLocaleString()}개 · 페이스: {views3D.faces?.toLocaleString()}개 · 마우스로 회전
            </p>
            <MeshViewer data={views3D.mesh} />
            <button onClick={() => setViews3D(null)}
              style={{ marginTop: '16px', width: '100%', padding: '12px', background: '#333', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer' }}>
              닫기
            </button>
          </div>
        </div>
      )}
      <div style={{ padding: '20px' }}>
        <h2>가구 재배치</h2>
        <p style={{ color: '#aaa' }}>드래그로 배치 · 클릭 후 모서리로 크기조절</p>
 
        <div style={{ display: 'flex', gap: '20px', marginTop: '20px' }}>
          {/* 가구 목록 */}
          <div style={{ width: '160px', background: '#1a1a1a', borderRadius: '12px', padding: '12px', overflowY: 'auto', maxHeight: '600px' }}>
            <h3 style={{ marginBottom: '12px', fontSize: '14px' }}>추출된 가구</h3>
            {loading ? <p style={{ color: '#666', fontSize: '13px' }}>추출 중...</p>
              : furnitureList.length === 0 ? <p style={{ color: '#666', fontSize: '13px' }}>가구 없음</p>
              : furnitureList.map((f, i) => (
                <div key={i} draggable
                  onDragStart={(e) => e.dataTransfer.setData('furnitureId', String(f.id))}
                  style={{ background: '#2a2a2a', borderRadius: '8px', padding: '8px', marginBottom: '8px', cursor: 'grab' }}>
                  <img src={`data:image/png;base64,${f.b64}`} style={{ width: '100%', borderRadius: '4px' }} />
                  <p style={{ fontSize: '11px', color: '#aaa', marginTop: '4px', textAlign: 'center' }}>{f.name || `가구 ${i+1}`}</p>
                  <button
                    onClick={(e) => generate3DViews(e, f)}
                    disabled={generating3D}
                    style={{ width: '100%', padding: '6px', marginTop: '4px', background: generating3D ? '#555' : '#3498db', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '11px' }}
                  >
                    {generating3D ? '생성 중...' : '🔄 3D 보기'}
                  </button>
                </div>
              ))
            }
          </div>
 
          {/* 캔버스 */}
          <div style={{ flex: 1 }}>
            <canvas ref={canvasRef}
              onMouseDown={handleMouseDown}
              onMouseMove={handleMouseMove}
              onMouseUp={handleMouseUp}
              onMouseLeave={handleMouseUp}
              onDragOver={(e) => e.preventDefault()}
              onDrop={handleDrop}
              style={{ width: '100%', borderRadius: '12px', border: '2px solid #333', cursor: 'default' }}
            />
          </div>
 
          {/* 컨트롤 */}
          <div style={{ width: '160px', background: '#1a1a1a', borderRadius: '12px', padding: '12px' }}>
            <h3 style={{ marginBottom: '12px', fontSize: '14px' }}>컨트롤</h3>
            {selectedId && (
              <button onClick={() => { setPlacedItems(prev => prev.filter(i => i.id !== selectedId)); setSelectedId(null) }}
                style={{ width: '100%', padding: '10px', marginBottom: '8px', background: '#e74c3c', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer' }}>
                🗑️ 선택 삭제
              </button>
            )}
            <button onClick={() => { setPlacedItems([]); setSelectedId(null) }}
              style={{ width: '100%', padding: '10px', marginBottom: '8px', background: '#333', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer' }}>
              🗑️ 전체 초기화
            </button>
            <button onClick={() => setPlacedItems(prev => prev.slice(0, -1))}
              style={{ width: '100%', padding: '10px', marginBottom: '8px', background: '#333', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer' }}>
              ↩ 되돌리기
            </button>
            <button onClick={reset}
              style={{ width: '100%', padding: '10px', background: '#e74c3c', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer' }}>
              🔄 처음부터
            </button>
            <button
              onClick={() => {
                const prevSelected = selectedId
                setSelectedId(null)
                setTimeout(() => {
                  const canvas = canvasRef.current
                  const link = document.createElement('a')
                  link.download = 'furniture_relocate.png'
                  link.href = canvas.toDataURL('image/png')
                  link.click()
                  toast.success('이미지 저장됐어요! 🎉')
                  setSelectedId(prevSelected)
                }, 100)
              }}
              style={{ width: '100%', padding: '10px', marginTop: '8px', background: '#27ae60', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer' }}
            >
              💾 사진 저장
            </button>
          </div>
        </div>
      </div>
    </>
  )
}