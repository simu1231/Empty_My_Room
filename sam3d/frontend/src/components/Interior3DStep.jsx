import { useRef, useEffect, useState, useMemo } from 'react'
import { useStore } from '../store/useStore'
import toast from 'react-hot-toast'

function MiniMeshViewer({ data }) {
  const mountRef = useRef(null)

  useEffect(() => {
    if (!data || !mountRef.current) return
    const THREE = window.THREE
    const el = mountRef.current
    const width = el.clientWidth
    const height = 120

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x2a2a2a)
    const camera = new THREE.PerspectiveCamera(45, width / height, 0.01, 1000)
    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setSize(width, height)
    el.appendChild(renderer.domElement)

    scene.add(new THREE.AmbientLight(0xffffff, 0.75))
    const dir = new THREE.DirectionalLight(0xffffff, 0.6)
    dir.position.set(4, 8, 5)
    scene.add(dir)

    let group
    if (data.type === 'procedural') {
      group = buildProceduralGroup(THREE, data)
      scene.add(group)
      const box = new THREE.Box3().setFromObject(group)
      const sz = box.getSize(new THREE.Vector3())
      const maxDim = Math.max(sz.x, sz.y, sz.z)
      camera.position.set(0, maxDim * 0.5, maxDim * 1.8)
      camera.lookAt(0, 0, 0)
    } else {
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute('position', new THREE.BufferAttribute(data.vertices, 3))
      geometry.setIndex(new THREE.BufferAttribute(data.faces, 1))

      let material
      if (data.type === 'textured') {
        geometry.setAttribute('uv', new THREE.BufferAttribute(data.uvs, 2))
        const texture = new THREE.TextureLoader().load(`data:image/jpeg;base64,${data.textureB64}`)
        texture.flipY = true
        material = new THREE.MeshBasicMaterial({ map: texture, side: THREE.DoubleSide })
      } else {
        geometry.setAttribute('color', new THREE.BufferAttribute(data.colors, 3))
        material = new THREE.MeshStandardMaterial({ vertexColors: true, side: THREE.DoubleSide, roughness: 0.5, metalness: 0.0 })
      }

      geometry.computeVertexNormals()
      geometry.center()
      geometry.computeBoundingBox()
      const size = new THREE.Vector3()
      geometry.boundingBox.getSize(size)
      const maxDim = Math.max(size.x, size.y, size.z)
      camera.position.set(0, maxDim * 0.5, maxDim * 1.5)
      camera.lookAt(0, 0, 0)

      const mesh = new THREE.Mesh(geometry, material)
      if (data.type !== 'textured') mesh.rotation.x = -Math.PI / 2
      group = new THREE.Group()
      group.add(mesh)
      scene.add(group)
    }

    let animId
    let angle = 0
    const animate = () => {
      animId = requestAnimationFrame(animate)
      angle += 0.01
      group.rotation.y = angle
      renderer.render(scene, camera)
    }
    animate()

    return () => {
      cancelAnimationFrame(animId)
      renderer.dispose()
      if (el.contains(renderer.domElement)) el.removeChild(renderer.domElement)
    }
  }, [data])

  return <div ref={mountRef} style={{ width: '100%', height: '120px', borderRadius: '6px', overflow: 'hidden' }} />
}

function RoomViewer({ roomSize, roomColors, roomTextures, placedMeshes, onDrop, onDelete, onCopy, viewMode }) {
  const createDynamicTexture = (baseColor, type = 'plank') => {
    const canvas = document.createElement('canvas')
    canvas.width = 512
    canvas.height = 512
    const ctx = canvas.getContext('2d')
    ctx.fillStyle = baseColor
    ctx.fillRect(0, 0, 512, 512)
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.15)'
    ctx.lineWidth = 2
    if (type === 'grid') {
      ctx.strokeStyle = 'rgba(0, 0, 0, 0.1)'
      for (let i = 0; i <= 512; i += 64) {
        ctx.beginPath(); ctx.moveTo(i, 0); ctx.lineTo(i, 512); ctx.stroke()
        ctx.beginPath(); ctx.moveTo(0, i); ctx.lineTo(512, i); ctx.stroke()
      }
    } else {
      ctx.strokeStyle = 'rgba(0, 0, 0, 0.07)'
      ctx.lineWidth = 1
      const plankWidth = 48
      for (let x = 0; x <= 512; x += plankWidth) {
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, 512); ctx.stroke()
        let startY = (x % (plankWidth * 2) === 0) ? 0 : 200
        for (let y = startY; y <= 512; y += 400) {
          ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + plankWidth, y); ctx.stroke()
        }
      }
    }
    const texture = new window.THREE.CanvasTexture(canvas)
    texture.wrapS = texture.wrapT = window.THREE.RepeatWrapping
    texture.repeat.set(2, 2)
    return texture
  }

  const mountRef = useRef(null)
  const sceneRef = useRef(null)
  const cameraRef = useRef(null)
  const rendererRef = useRef(null)
  const placedGroupsRef = useRef({})
  const selectedObjRef = useRef(null)
  const floorRef = useRef(null)
  const backWallRef = useRef(null)
  const leftWallRef = useRef(null)
  const rightWallRef = useRef(null)
  const viewModeRef = useRef('3d')
  const camTargetRef = useRef({ x: 0, y: 0, z: 0 })
  const [contextMenu, setContextMenu] = useState(null)
  // contextMenu: { screenX, screenY, instanceId }

  useEffect(() => {
    if (!mountRef.current) return
    const THREE = window.THREE
    console.log(roomSize)
    console.log('wallColor:', roomColors.wall)  
    console.log('floorColor:', roomColors.floor)  
    const el = mountRef.current
    const width = el.clientWidth || window.innerWidth
    const height = el.clientHeight || window.innerHeight

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x2a2a2a)
    sceneRef.current = scene

    const camera = new THREE.PerspectiveCamera(45, width / height, 0.01, 1000)
    camera.position.set(0, roomSize.height * 1.5, roomSize.depth * 2.5)
    camera.lookAt(0, 0, 0)
    cameraRef.current = camera

    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true })
    renderer.setSize(width, height)
    renderer.shadowMap.enabled = false
    renderer.outputColorSpace = THREE.SRGBColorSpace
    el.appendChild(renderer.domElement)
    rendererRef.current = renderer
    const raycaster = new THREE.Raycaster()

    scene.add(new THREE.AmbientLight(0xffffff, 0.75))
    const sunLight = new THREE.DirectionalLight(0xfff8f0, 0.5)
    sunLight.position.set(5, 10, 8)
    scene.add(sunLight)

    const w = roomSize.width
    const h = roomSize.height
    const d = roomSize.depth
    const wc = roomColors.wall
    const fc = roomColors.floor
    const fColor = `rgb(${fc[0]*255}, ${fc[1]*255}, ${fc[2]*255})`

  const floor = new THREE.Mesh(
  new THREE.PlaneGeometry(w, d),
  new THREE.MeshStandardMaterial({ map: createDynamicTexture(fColor, 'plank'), roughness: 0.8 })
)
    floor.rotation.x = -Math.PI / 2
    floor.receiveShadow = true
    floor.position.y = -h / 2
    floor.name = 'floor'
    scene.add(floor)
    floorRef.current = floor
    console.log('wc:', wc)
    console.log('fc:', fc)
    
    const wallMaterial = new THREE.MeshStandardMaterial({ 
  color: new THREE.Color(wc[0], wc[1], wc[2]), 
  roughness: 1.0 
})
    const backWall = new THREE.Mesh(new THREE.PlaneGeometry(w, h), wallMaterial)
    backWall.position.z = -d / 2
    backWall.name = 'wall_back'
    scene.add(backWall)
    backWallRef.current = backWall

    const leftWall = new THREE.Mesh(new THREE.PlaneGeometry(d, h), wallMaterial)
    leftWall.rotation.y = Math.PI / 2
    leftWall.position.x = -w / 2
    leftWall.name = 'wall_left'
    scene.add(leftWall)
    leftWallRef.current = leftWall

    const rightWall = new THREE.Mesh(new THREE.PlaneGeometry(d, h), wallMaterial)
    rightWall.rotation.y = -Math.PI / 2
    rightWall.position.x = w / 2
    rightWall.name = 'wall_right'
    scene.add(rightWall)
    rightWallRef.current = rightWall

    const ceiling = new THREE.Mesh(
      new THREE.PlaneGeometry(w, d),
      new THREE.MeshStandardMaterial({ color: 0xffffff })
    )
    ceiling.rotation.x = Math.PI / 2
    ceiling.position.y = h / 2
    scene.add(ceiling)

    const getMousePos = (e) => {
      const rect = el.getBoundingClientRect()
      return {
        x: ((e.clientX - rect.left) / rect.width) * 2 - 1,
        y: -((e.clientY - rect.top) / rect.height) * 2 + 1,
      }
    }

    let isRotating = false
    let prevX = 0, prevY = 0
    let isDraggingFurniture = false
    let mouseDownX = 0, mouseDownY = 0  // 드래그 판별용

    const onMouseDown = (e) => {
      mouseDownX = e.clientX
      mouseDownY = e.clientY

      const mouse = getMousePos(e)
      raycaster.setFromCamera(mouse, camera)
      const entries = Object.entries(placedGroupsRef.current)
      const placedObjects = entries.map(([, g]) => g)
      const intersects = raycaster.intersectObjects(placedObjects, true)

      if (intersects.length > 0 && e.button === 0) {
        let obj = intersects[0].object
        while (obj.parent && !placedObjects.includes(obj)) obj = obj.parent
        selectedObjRef.current = obj
        isDraggingFurniture = true
        setContextMenu(null)
      } else {
        isRotating = true
        prevX = e.clientX
        prevY = e.clientY
        setContextMenu(null)
      }
    }

    const onMouseMove = (e) => {
  if (isDraggingFurniture && selectedObjRef.current) {
    const mouse = getMousePos(e)
    raycaster.setFromCamera(mouse, camera)

    // 벽 아이템: 해당 벽면에서만 이동
    if (selectedObjRef.current.userData.isWallItem) {
      const wn = selectedObjRef.current.userData.wallNormal
      const wallMesh = wn === 'back' ? backWallRef.current
        : wn === 'left'  ? leftWallRef.current
        : rightWallRef.current
      if (wallMesh) {
        const hits = raycaster.intersectObject(wallMesh)
        if (hits.length > 0) {
          const pt = hits[0].point
          if (wn === 'back')        { selectedObjRef.current.position.x = pt.x; selectedObjRef.current.position.y = pt.y }
          else if (wn === 'left')   { selectedObjRef.current.position.z = pt.z; selectedObjRef.current.position.y = pt.y }
          else if (wn === 'right')  { selectedObjRef.current.position.z = pt.z; selectedObjRef.current.position.y = pt.y }
        }
      }
      return
    }

    const intersects = raycaster.intersectObject(floorRef.current)
    if (intersects.length > 0) {
      // 벽 딱 붙이기: 아주 작은 여백(2cm)만 남김
      const halfW = roomSize.width / 2 - 0.02
      const halfD = roomSize.depth / 2 - 0.02

      // 벽 통과 방지 클램프
      let newX = Math.max(-halfW, Math.min(halfW, intersects[0].point.x))
      let newZ = Math.max(-halfD, Math.min(halfD, intersects[0].point.z))

      // 가구끼리 겹침 방지
      const currentId = Object.entries(placedGroupsRef.current)
        .find(([, g]) => g === selectedObjRef.current)?.[0]

      /*Object.entries(placedGroupsRef.current).forEach(([id, otherGroup]) => {
        if (id === currentId) return
        const dx = newX - otherGroup.position.x
        const dz = newZ - otherGroup.position.z
        const dist = Math.sqrt(dx * dx + dz * dz)
        const minDist = 0.8
        if (dist < minDist && dist > 0) {
          const nx = dx / dist
          const nz = dz / dist
          newX = otherGroup.position.x + nx * minDist
          newZ = otherGroup.position.z + nz * minDist
        }
      })*/

      selectedObjRef.current.position.x = newX
      selectedObjRef.current.position.z = newZ
    }
  } else if (isRotating) {
    const dx = e.clientX - prevX
    const dy = e.clientY - prevY
    if (viewModeRef.current === '2d') {
      const s = 0.02
      camTargetRef.current.x -= dx * s
      camTargetRef.current.z -= dy * s
      camera.position.x -= dx * s
      camera.position.z -= dy * s
      camera.lookAt(camTargetRef.current.x, 0, camTargetRef.current.z)
    } else {
      const spherical = new THREE.Spherical().setFromVector3(camera.position)
      spherical.theta -= dx * 0.01
      spherical.phi -= dy * 0.005
      spherical.phi = Math.max(0.1, Math.min(Math.PI / 2, spherical.phi))
      camera.position.setFromSpherical(spherical)
      camera.lookAt(0, 0, 0)
    }
    prevX = e.clientX
    prevY = e.clientY
  }
}

    const onMouseUp = (e) => {

      const movedX = Math.abs(e.clientX - mouseDownX)
      const movedY = Math.abs(e.clientY - mouseDownY)
      const wasDrag = movedX > 5 || movedY > 5

      // 드래그 안 했고 가구 클릭이면 컨텍스트 메뉴 표시
      if (!wasDrag && isDraggingFurniture && selectedObjRef.current) {
        const entries = Object.entries(placedGroupsRef.current)
        const instanceId = entries.find(([, g]) => g === selectedObjRef.current)?.[0]
        setContextMenu({ screenX: e.clientX, screenY: e.clientY, instanceId })
      }

      isDraggingFurniture = false
      isRotating = false
    }

    const onWheel = (e) => {
      e.preventDefault()
      const spherical = new THREE.Spherical().setFromVector3(camera.position)
      spherical.radius += e.deltaY * 0.01
      spherical.radius = Math.max(1, Math.min(20, spherical.radius))
      camera.position.setFromSpherical(spherical)
      camera.lookAt(0, 0, 0)
    }

    renderer.domElement.addEventListener('mousedown', onMouseDown)
    renderer.domElement.addEventListener('mousemove', onMouseMove)
    renderer.domElement.addEventListener('mouseup', onMouseUp)
    renderer.domElement.addEventListener('wheel', onWheel, { passive: false })

    let animId
    const animate = () => { animId = requestAnimationFrame(animate); renderer.render(scene, camera) }
    animate()

    return () => {
      cancelAnimationFrame(animId)
      renderer.domElement.removeEventListener('wheel', onWheel)
      renderer.dispose()
      if (el.contains(renderer.domElement)) el.removeChild(renderer.domElement)
    }
  }, [roomSize, roomColors, roomTextures])

useEffect(() => {
  if (!sceneRef.current) return
  const THREE = window.THREE

  // undo/redo 시 사라진 가구 Three.js 씬에서도 제거
  Object.entries(placedGroupsRef.current).forEach(([instanceId, group]) => {
    if (!placedMeshes[instanceId]) {
      sceneRef.current.remove(group)
      delete placedGroupsRef.current[instanceId]
    }
  })

  Object.entries(placedMeshes).forEach(([instanceId, { data, position, estimatedRealSize }]) => {
    if (placedGroupsRef.current[instanceId]) return

    let group

    if (data.type === 'procedural') {
      group = buildProceduralGroup(THREE, data)
      if (position.wallNormal) {
        // 벽 부착
        const wn = position.wallNormal
        const wallOffset = 0.04
        group.position.set(
          wn === 'left'  ? -roomSize.width / 2 + wallOffset
            : wn === 'right' ? roomSize.width / 2 - wallOffset : position.x,
          position.y,
          wn === 'back'  ? -roomSize.depth / 2 + wallOffset : position.z
        )
        group.rotation.y = wn === 'left' ? Math.PI / 2 : wn === 'right' ? -Math.PI / 2 : 0
        group.userData.isWallItem = true
        group.userData.wallNormal = wn
        group.userData.halfSize = 0
      } else {
        group.position.set(position.x, -roomSize.height / 2 + data.halfH, position.z)
        group.userData.halfSize = data.halfH
      }
      group.userData.halfFX = data.halfFX
      group.userData.halfFZ = data.halfFZ
    } else {
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute('position', new THREE.BufferAttribute(data.vertices, 3))
      geometry.setIndex(new THREE.BufferAttribute(data.faces, 1))

      let material
      if (data.type === 'textured') {
        geometry.setAttribute('uv', new THREE.BufferAttribute(data.uvs, 2))
        const texture = new THREE.TextureLoader().load(`data:image/jpeg;base64,${data.textureB64}`)
        texture.flipY = true
        material = new THREE.MeshBasicMaterial({ map: texture, side: THREE.DoubleSide })
      } else {
        geometry.setAttribute('color', new THREE.BufferAttribute(data.colors, 3))
        material = new THREE.MeshStandardMaterial({ vertexColors: true, side: THREE.DoubleSide, roughness: 0.5, metalness: 0.0 })
      }

      geometry.computeVertexNormals()
      geometry.center()
      geometry.computeBoundingBox()
      const size = new THREE.Vector3()
      geometry.boundingBox.getSize(size)
      const maxDim = Math.max(size.x, size.y, size.z)

      const std = findStdSize(data.name)
      let scaleX, scaleY, scaleZ, scaledHalfHeight

      if (std && data.type !== 'textured') {
        scaleX = size.x > 0 ? std.w / size.x : 1
        scaleY = size.y > 0 ? std.d / size.y : 1
        scaleZ = size.z > 0 ? std.h / size.z : 1
        scaledHalfHeight = std.h / 2
      } else if (std && data.type === 'textured') {
        scaleX = size.x > 0 ? std.w / size.x : 1
        scaleY = size.y > 0 ? std.h / size.y : 1
        scaleZ = size.z > 0 ? std.d / size.z : 1
        scaledHalfHeight = std.h / 2
      } else {
        const targetSize = estimatedRealSize || roomSize.width * 0.2
        const s = targetSize / maxDim
        scaleX = s; scaleY = s; scaleZ = s
        scaledHalfHeight = data.type === 'textured' ? (size.y * s) / 2 : (size.z * s) / 2
      }

      const mesh = new THREE.Mesh(geometry, material)
      mesh.castShadow = true
      mesh.receiveShadow = true
      if (data.type !== 'textured') mesh.rotation.x = -Math.PI / 2
      mesh.scale.set(scaleX, scaleY, scaleZ)

      const halfFX = (size.x * scaleX) / 2
      const halfFZ = data.type !== 'textured' ? (size.y * scaleY) / 2 : (size.z * scaleZ) / 2

      group = new THREE.Group()
      group.add(mesh)

      if (position.wallNormal) {
        const wn = position.wallNormal
        const wallOffset = 0.04
        group.position.set(
          wn === 'left'  ? -roomSize.width / 2 + wallOffset
            : wn === 'right' ? roomSize.width / 2 - wallOffset : position.x,
          position.y,
          wn === 'back'  ? -roomSize.depth / 2 + wallOffset : position.z
        )
        group.rotation.y = wn === 'left' ? Math.PI / 2 : wn === 'right' ? -Math.PI / 2 : 0
        group.userData.isWallItem = true
        group.userData.wallNormal = wn
        group.userData.halfSize = 0
      } else {
        group.position.set(position.x, -roomSize.height / 2 + scaledHalfHeight, position.z)
        group.userData.halfSize = scaledHalfHeight
      }
      group.userData.halfFX = halfFX
      group.userData.halfFZ = halfFZ

    }

    sceneRef.current.add(group)
    placedGroupsRef.current[instanceId] = group
  })
}, [placedMeshes])

  // 2D/3D 전환 시 카메라 재배치
  useEffect(() => {
    viewModeRef.current = viewMode
    if (!cameraRef.current) return
    if (viewMode === '2d') {
      const h = Math.max(roomSize.width, roomSize.depth) * 1.5
      cameraRef.current.position.set(camTargetRef.current.x, h, camTargetRef.current.z + 0.001)
      cameraRef.current.lookAt(camTargetRef.current.x, 0, camTargetRef.current.z)
    } else {
      camTargetRef.current = { x: 0, y: 0, z: 0 }
      cameraRef.current.position.set(0, roomSize.height * 1.5, roomSize.depth * 2.5)
      cameraRef.current.lookAt(0, 0, 0)
    }
  }, [viewMode])

  const handleDragOver = (e) => e.preventDefault()

  const handleDrop = (e) => {
    e.preventDefault()
    const furnitureId = Number(e.dataTransfer.getData('furnitureId'))
    const furnitureName = e.dataTransfer.getData('furnitureName') || ''
    const isWallItem = [...WALL_ITEM_NAMES].some(k => furnitureName.includes(k))
    const THREE = window.THREE
    const raycaster = new THREE.Raycaster()
    const rect = mountRef.current.getBoundingClientRect()
    const nx = ((e.clientX - rect.left) / rect.width) * 2 - 1
    const ny = -((e.clientY - rect.top) / rect.height) * 2 + 1
    if (!cameraRef.current) return
    raycaster.setFromCamera({ x: nx, y: ny }, cameraRef.current)

    if (isWallItem) {
      const walls = [backWallRef.current, leftWallRef.current, rightWallRef.current].filter(Boolean)
      const hits = raycaster.intersectObjects(walls)
      if (hits.length > 0) {
        const hit = hits[0]
        const name = hit.object.name  // 'wall_back' | 'wall_left' | 'wall_right'
        const wallNormal = name === 'wall_back' ? 'back' : name === 'wall_left' ? 'left' : 'right'
        onDrop(furnitureId, { x: hit.point.x, y: hit.point.y, z: hit.point.z, wallNormal })
      }
    } else if (floorRef.current) {
      const hits = raycaster.intersectObject(floorRef.current)
      if (hits.length > 0) {
        onDrop(furnitureId, { x: hits[0].point.x, z: hits[0].point.z })
      }
    }
  }

  // 컨텍스트 메뉴 액션들
  const handleRotate = () => {
    if (selectedObjRef.current) {
      selectedObjRef.current.rotation.y += Math.PI / 2
    }
    setContextMenu(null)
  }

  const handleDelete = () => {
    if (contextMenu?.instanceId) {
      const group = placedGroupsRef.current[contextMenu.instanceId]
      if (group && sceneRef.current) {
        sceneRef.current.remove(group)
        delete placedGroupsRef.current[contextMenu.instanceId]
      }
      onDelete(contextMenu.instanceId)
    }
    setContextMenu(null)
  }

  const handleCopy = () => {
    if (contextMenu?.instanceId) {
      onCopy(contextMenu.instanceId)
    }
    setContextMenu(null)
  }

  const handleScale = (factor) => {
    if (selectedObjRef.current) {
      const group = selectedObjRef.current
      group.scale.multiplyScalar(factor)
      const newHs = (group.userData.halfSize || 0.5) * factor
      group.userData.halfSize = newHs
      group.userData.halfFX = (group.userData.halfFX || newHs) * factor
      group.userData.halfFZ = (group.userData.halfFZ || newHs) * factor
      // 벽 아이템은 벽에서 떨어지지 않게 y 보정 안 함
      if (!group.userData.isWallItem) {
        group.position.y = -roomSize.height / 2 + newHs
      }
    }
  }

  const btnStyle = (color) => ({
    width: '48px', height: '48px', borderRadius: '50%',
    background: color, border: 'none', cursor: 'pointer',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontSize: '20px', boxShadow: '0 2px 8px rgba(0,0,0,0.4)',
    transition: 'transform 0.1s',
  })

  return (
    <div
      ref={mountRef}
      style={{ width: '100%', height: '100%', cursor: 'grab', position: 'relative' }}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      {/* 컨텍스트 메뉴 */}
      {contextMenu && (
        <div style={{
          position: 'fixed',
          left: contextMenu.screenX - 80,
          top: contextMenu.screenY - 20,
          zIndex: 100,
          display: 'flex', gap: '8px',
          background: 'rgba(20,20,20,0.85)',
          backdropFilter: 'blur(10px)',
          borderRadius: '30px',
          padding: '8px 12px',
          border: '1px solid rgba(255,255,255,0.1)',
        }}>
          <button style={btnStyle('#3498db')} onClick={handleRotate} title="회전">🔄</button>
          <button style={btnStyle('#27ae60')} onClick={handleCopy} title="복사">📋</button>
          <button style={btnStyle('#e67e22')} onClick={() => handleScale(1.2)} title="크게">＋</button>
          <button style={btnStyle('#e67e22')} onClick={() => handleScale(1/1.2)} title="작게">－</button>
          <button style={btnStyle('#e74c3c')} onClick={handleDelete} title="삭제">🗑️</button>
        </div>
      )}
    </div>
  )
}

// 가구별 표준 크기 (단위: 미터)
// vertex_color 메쉬 기준: local.x→worldX(폭), local.y→worldZ(깊이), local.z→worldY(높이)
const FURNITURE_STD_SIZES = {
  '킹침대':    { w: 1.8, d: 2.1, h: 0.55 },
  '퀸침대':    { w: 1.6, d: 2.0, h: 0.55 },
  '더블침대':  { w: 1.4, d: 2.0, h: 0.55 },
  '싱글침대':  { w: 1.0, d: 2.0, h: 0.55 },
  '침대':      { w: 1.6, d: 2.0, h: 0.55 },
  '3인소파':   { w: 2.1, d: 0.9, h: 0.85 },
  '2인소파':   { w: 1.5, d: 0.85, h: 0.85 },
  '1인소파':   { w: 0.85, d: 0.85, h: 0.85 },
  '소파':      { w: 2.0, d: 0.9, h: 0.85 },
  '옷장':      { w: 1.2, d: 0.6, h: 2.0 },
  '책장':      { w: 0.9, d: 0.3, h: 1.8 },
  '책상':      { w: 1.2, d: 0.6, h: 0.75 },
  '식탁':      { w: 1.4, d: 0.8, h: 0.75 },
  '테이블':    { w: 1.2, d: 0.7, h: 0.75 },
  '협탁':      { w: 0.5, d: 0.4, h: 0.6 },
  '화장대':    { w: 1.0, d: 0.5, h: 0.75 },
  '의자':      { w: 0.5, d: 0.5, h: 0.9 },
  '텔레비전':  { w: 1.2, d: 0.1, h: 0.7 },
  'TV':        { w: 1.2, d: 0.1, h: 0.7 },
}

// 가구 이름에서 표준 크기 검색 (긴 키 우선으로 부분 매칭)
function findStdSize(name) {
  if (!name) return null
  if (FURNITURE_STD_SIZES[name]) return FURNITURE_STD_SIZES[name]
  const sorted = Object.entries(FURNITURE_STD_SIZES).sort((a, b) => b[0].length - a[0].length)
  for (const [key, val] of sorted) {
    if (name.includes(key)) return val
  }
  return null
}

// 가구별 프로시저 모델 정의 (y=0이 바닥 기준, halfH만큼 내려서 중심으로 맞춤)
const PROCEDURAL_FURNITURE = {
  '의자': { w:0.5, d:0.5, h:0.9, parts:[
    { size:[0.45,0.05,0.45], pos:[0,0.45,0],    c:[0.85,0.76,0.62] },
    { size:[0.44,0.48,0.05], pos:[0,0.68,-0.2], c:[0.85,0.76,0.62] },
    { size:[0.04,0.44,0.04], pos:[-0.19,0.22, 0.18], c:[0.68,0.58,0.44] },
    { size:[0.04,0.44,0.04], pos:[ 0.19,0.22, 0.18], c:[0.68,0.58,0.44] },
    { size:[0.04,0.44,0.04], pos:[-0.19,0.22,-0.18], c:[0.68,0.58,0.44] },
    { size:[0.04,0.44,0.04], pos:[ 0.19,0.22,-0.18], c:[0.68,0.58,0.44] },
  ]},
  '침대': { w:1.6, d:2.0, h:0.55, parts:[
    { size:[1.60,0.24,2.00], pos:[0,0.12, 0],        c:[0.58,0.48,0.38] }, // 프레임
    { size:[1.50,0.20,1.85], pos:[0,0.34, 0.05],     c:[0.96,0.94,0.91] }, // 매트리스
    { size:[1.60,0.52,0.09], pos:[0,0.40,-0.955],    c:[0.52,0.42,0.32] }, // 헤드보드
    { shape:'pillow', size:[0.58,0.12,0.36], pos:[-0.34,0.47,-0.68], c:[0.97,0.96,0.94] }, // 베개 L
    { shape:'pillow', size:[0.58,0.12,0.36], pos:[ 0.34,0.47,-0.68], c:[0.97,0.96,0.94] }, // 베개 R
    { size:[1.48,0.11,1.15], pos:[0,0.43, 0.38],     c:[0.72,0.78,0.90] }, // 이불
    { size:[1.46,0.06,0.30], pos:[0,0.50, 0.93],     c:[0.80,0.85,0.96] }, // 이불 접힌 부분
  ]},
  '소파': { w:2.0, d:0.9, h:0.85, parts:[
    { size:[2.00,0.34,0.85], pos:[0,0.17, 0],     c:[0.40,0.36,0.52] },
    { size:[2.00,0.20,0.70], pos:[0,0.44, 0.06],  c:[0.54,0.48,0.66] },
    { size:[2.00,0.50,0.14], pos:[0,0.59,-0.38],  c:[0.46,0.41,0.58] },
    { size:[0.15,0.50,0.85], pos:[-0.93,0.54, 0], c:[0.40,0.36,0.52] },
    { size:[0.15,0.50,0.85], pos:[ 0.93,0.54, 0], c:[0.40,0.36,0.52] },
  ]},
  '옷장': { w:1.2, d:0.6, h:2.0, parts:[
    { size:[1.18,1.96,0.56], pos:[0,0.98, 0],    c:[0.84,0.81,0.74] },
    { size:[0.57,1.86,0.03], pos:[-0.29,0.95, 0.29], c:[0.91,0.88,0.82] },
    { size:[0.57,1.86,0.03], pos:[ 0.29,0.95, 0.29], c:[0.91,0.88,0.82] },
    { size:[1.20,0.04,0.60], pos:[0,1.98, 0],    c:[0.72,0.69,0.62] },
    { size:[0.02,0.04,0.02], pos:[-0.08,0.95,0.32], c:[0.55,0.45,0.35] },
    { size:[0.02,0.04,0.02], pos:[ 0.08,0.95,0.32], c:[0.55,0.45,0.35] },
  ]},
  '서랍장': { w:0.8, d:0.5, h:1.0, parts:[
    { size:[0.78,0.96,0.48], pos:[0,0.48, 0],    c:[0.84,0.81,0.74] },
    { size:[0.72,0.22,0.42], pos:[0,0.12,0.25],  c:[0.91,0.88,0.82] },
    { size:[0.72,0.22,0.42], pos:[0,0.37,0.25],  c:[0.91,0.88,0.82] },
    { size:[0.72,0.22,0.42], pos:[0,0.62,0.25],  c:[0.91,0.88,0.82] },
    { size:[0.80,0.03,0.50], pos:[0,0.995,0],    c:[0.72,0.69,0.62] },
    { size:[0.04,0.02,0.02], pos:[0,0.12,0.47],  c:[0.55,0.45,0.35] },
    { size:[0.04,0.02,0.02], pos:[0,0.37,0.47],  c:[0.55,0.45,0.35] },
    { size:[0.04,0.02,0.02], pos:[0,0.62,0.47],  c:[0.55,0.45,0.35] },
  ]},
  '책상': { w:1.2, d:0.6, h:0.75, parts:[
    { size:[1.20,0.04,0.60], pos:[0,0.74, 0],    c:[0.72,0.62,0.48] },
    { size:[0.04,0.72,0.04], pos:[-0.57,0.36, 0.27], c:[0.62,0.52,0.40] },
    { size:[0.04,0.72,0.04], pos:[ 0.57,0.36, 0.27], c:[0.62,0.52,0.40] },
    { size:[0.04,0.72,0.04], pos:[-0.57,0.36,-0.27], c:[0.62,0.52,0.40] },
    { size:[0.04,0.72,0.04], pos:[ 0.57,0.36,-0.27], c:[0.62,0.52,0.40] },
  ]},
  '협탁': { w:0.5, d:0.4, h:0.6, parts:[
    { size:[0.48,0.56,0.38], pos:[0,0.28, 0],    c:[0.84,0.81,0.74] },
    { size:[0.42,0.24,0.32], pos:[0,0.15,0.20],  c:[0.91,0.88,0.82] },
    { size:[0.50,0.03,0.40], pos:[0,0.595,0],    c:[0.72,0.69,0.62] },
    { size:[0.03,0.02,0.02], pos:[0,0.15,0.38],  c:[0.55,0.45,0.35] },
  ]},
  '책장': { w:0.9, d:0.3, h:1.8, parts:[
    { size:[0.88,1.76,0.28], pos:[0,0.88, 0],    c:[0.84,0.81,0.74] },
    { size:[0.84,0.02,0.26], pos:[0,0.36, 0],    c:[0.91,0.88,0.82] },
    { size:[0.84,0.02,0.26], pos:[0,0.72, 0],    c:[0.91,0.88,0.82] },
    { size:[0.84,0.02,0.26], pos:[0,1.08, 0],    c:[0.91,0.88,0.82] },
    { size:[0.84,0.02,0.26], pos:[0,1.44, 0],    c:[0.91,0.88,0.82] },
    { size:[0.90,0.03,0.30], pos:[0,1.795,0],    c:[0.72,0.69,0.62] },
  ]},
  // 벽 부착 아이템 (parts y=0이 아이템 중심, wallItem: true)
  // 창문: 프레임 5개 + 반투명 유리 2장 + 외부 스카이 힌트
  '창문': { w:1.2, d:0.10, h:1.0, wallItem:true, parts:[
    // ── 프레임 (불투명) ──
    { size:[1.20,0.07,0.10], pos:[0, 0.465,0],  c:[0.90,0.90,0.90] }, // 상단 레일
    { size:[1.20,0.07,0.10], pos:[0,-0.465,0],  c:[0.90,0.90,0.90] }, // 하단 레일
    { size:[0.07,0.86,0.10], pos:[-0.565,0,0],  c:[0.90,0.90,0.90] }, // 좌측 기둥
    { size:[0.07,0.86,0.10], pos:[ 0.565,0,0],  c:[0.90,0.90,0.90] }, // 우측 기둥
    { size:[0.05,0.86,0.08], pos:[0,0,0],       c:[0.90,0.90,0.90] }, // 중간 멀리언
    // ── 유리 (반투명 하늘색, 빛 방출로 외부 느낌) ──
    { size:[0.47,0.84,0.01], pos:[-0.28,0,0.01], c:[0.72,0.88,0.97], transparent:true, opacity:0.30, emissive:[0.35,0.55,0.75], emissiveIntensity:0.25 },
    { size:[0.47,0.84,0.01], pos:[ 0.28,0,0.01], c:[0.72,0.88,0.97], transparent:true, opacity:0.30, emissive:[0.35,0.55,0.75], emissiveIntensity:0.25 },
    // ── 창틀 내측 깊이 (어두운 박스로 두께감) ──
    { size:[1.06,0.86,0.02], pos:[0,0,-0.04],   c:[0.20,0.20,0.20] },
  ]},
  '액자': { w:0.8, d:0.06, h:0.6, wallItem:true, parts:[
    { size:[0.80,0.60,0.06], pos:[0,0,0],     c:[0.35,0.26,0.18] },  // 바깥 틀
    { size:[0.68,0.48,0.01], pos:[0,0,0.032], c:[0.92,0.86,0.74] },  // 그림 (따뜻한 크림)
    { size:[0.04,0.48,0.04], pos:[-0.32,0,0.015], c:[0.30,0.22,0.14] }, // 내측 좌
    { size:[0.04,0.48,0.04], pos:[ 0.32,0,0.015], c:[0.30,0.22,0.14] }, // 내측 우
    { size:[0.72,0.04,0.04], pos:[0, 0.28,0.015], c:[0.30,0.22,0.14] }, // 내측 상
    { size:[0.72,0.04,0.04], pos:[0,-0.28,0.015], c:[0.30,0.22,0.14] }, // 내측 하
  ]},
  '조명': { w:0.35, d:0.20, h:0.35, wallItem:true, parts:[
    { size:[0.12,0.18,0.10], pos:[0, 0.10,-0.04], c:[0.75,0.72,0.65] }, // 브래킷
    { size:[0.30,0.22,0.22], pos:[0,-0.02, 0.06], c:[0.95,0.92,0.85] }, // 갓
    { size:[0.08,0.08,0.06], pos:[0,-0.02, 0.05], c:[1.0, 0.98,0.85]  }, // 전구
  ]},
}

function findProceduralDef(name) {
  if (!name) return null
  if (PROCEDURAL_FURNITURE[name]) return PROCEDURAL_FURNITURE[name]
  const sorted = Object.entries(PROCEDURAL_FURNITURE).sort((a, b) => b[0].length - a[0].length)
  for (const [key, val] of sorted) {
    if (name.includes(key)) return val
  }
  return null
}

// wallItem: parts y=0이 이미 중심 → halfH 보정 없음
// 일반 가구: parts y=0이 바닥 → halfH만큼 내려서 중심 맞춤
function buildProceduralGroup(THREE, data) {
  const group = new THREE.Group()
  const yOffset = data.wallItem ? 0 : -data.halfH
  data.parts.forEach(p => {
    // pillow: 구체를 눌러서 부드러운 베개 모양
    const geo = p.shape === 'pillow'
      ? new THREE.SphereGeometry(0.5, 24, 14)
      : new THREE.BoxGeometry(...p.size)
    let mat
    if (p.transparent) {
      // 유리 등 반투명 재질
      mat = new THREE.MeshPhysicalMaterial({
        color: new THREE.Color(...p.c),
        transparent: true,
        opacity: p.opacity ?? 0.3,
        roughness: 0.0,
        metalness: 0.0,
        emissive: p.emissive ? new THREE.Color(...p.emissive) : new THREE.Color(0, 0, 0),
        emissiveIntensity: p.emissiveIntensity ?? 0,
        side: THREE.DoubleSide,
        depthWrite: false,
      })
    } else {
      mat = new THREE.MeshStandardMaterial({
        color: new THREE.Color(...p.c),
        roughness: 0.42,
        metalness: 0.02,
      })
    }
    const mesh = new THREE.Mesh(geo, mat)
    mesh.position.set(p.pos[0], p.pos[1] + yOffset, p.pos[2])
    if (p.shape === 'pillow') mesh.scale.set(p.size[0], p.size[1], p.size[2])
    group.add(mesh)
  })
  return group
}

// 벽에 붙이는 아이템 이름 목록
const WALL_ITEM_NAMES = new Set(['창문', '액자', '조명', '그림', '거울'])

// b64 이미지에서 픽셀 크기를 비동기로 읽어옴
function getImageSize(b64) {
  return new Promise(resolve => {
    const img = new Image()
    img.onload = () => resolve({ w: img.naturalWidth, h: img.naturalHeight })
    img.onerror = () => resolve({ w: 100, h: 100 })
    img.src = `data:image/png;base64,${b64}`
  })
}

// IndexedDB 헬퍼
const DB_NAME = 'EmptyMyRoomDesigns', DB_VER = 1, STORE = 'designs'
function openDB() {
  return new Promise((res, rej) => {
    const req = indexedDB.open(DB_NAME, DB_VER)
    req.onupgradeneeded = e => e.target.result.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true })
    req.onsuccess = e => res(e.target.result)
    req.onerror = e => rej(e.target.error)
  })
}
async function dbSaveDesign(data) {
  const db = await openDB()
  return new Promise((res, rej) => {
    const tx = db.transaction(STORE, 'readwrite')
    const req = tx.objectStore(STORE).add(data)
    req.onsuccess = () => res(req.result)
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

export default function Interior3DStep() {
  const { furnitureList, roomSize, roomColors, roomTextures, reset,
          savedDesignToLoad, clearSavedDesignToLoad, setSavedDesignToLoad, setStep } = useStore()
  const roomColorsMemo = useMemo(() => ({
    wall: roomColors?.wall || [0.9, 0.9, 0.9],
    floor: roomColors?.floor || [0.6, 0.4, 0.2]
  }), [roomColors])

  const [furnitureMeshes, setFurnitureMeshes] = useState({})
  // ─── Undo/Redo ───────────────────────────────────────────────────────────
  const [undoState, setUndoState] = useState({ snapshots: [{}], idx: 0 })
  const placedMeshes = undoState.snapshots[undoState.idx]
  const setPlacedMeshes = (updater) => setUndoState(prev => {
    const cur = prev.snapshots[prev.idx]
    const next = typeof updater === 'function' ? updater(cur) : updater
    const snaps = [...prev.snapshots.slice(0, prev.idx + 1), next].slice(-30)
    return { snapshots: snaps, idx: snaps.length - 1 }
  })
  const undo = () => setUndoState(p => p.idx > 0 ? { ...p, idx: p.idx - 1 } : p)
  const redo = () => setUndoState(p => p.idx < p.snapshots.length - 1 ? { ...p, idx: p.idx + 1 } : p)
  const canUndo = undoState.idx > 0
  const canRedo = undoState.idx < undoState.snapshots.length - 1
  // ─────────────────────────────────────────────────────────────────────────
  const [viewMode, setViewMode] = useState('3d')
  const [generating, setGenerating] = useState(false)
  const [generatingId, setGeneratingId] = useState(null)
  const [furniturePxSizes, setFurniturePxSizes] = useState({})

  // 저장된 디자인 불러오기
  useEffect(() => {
    if (!savedDesignToLoad) return
    setFurnitureMeshes(savedDesignToLoad.furnitureMeshes)
    setUndoState({ snapshots: [savedDesignToLoad.placedMeshes], idx: 0 })
    clearSavedDesignToLoad()
  }, [savedDesignToLoad])

  // 저장 함수
  const handleSave = async () => {
    const canvas = document.querySelector('#room-viewer-wrap canvas')
    if (!canvas) { toast.error('뷰어를 찾을 수 없습니다'); return }
    const thumbnail = canvas.toDataURL('image/jpeg', 0.6)
    try {
      await dbSaveDesign({
        timestamp: Date.now(),
        thumbnail,
        furnitureMeshes,
        placedMeshes,
      })
      toast.success('디자인 저장 완료! 업로드 화면에서 불러올 수 있습니다')
    } catch (e) {
      toast.error('저장 실패: ' + e.message)
    }
  }

  useEffect(() => {
    if (!furnitureList?.length) return
    Promise.all(furnitureList.map(f => getImageSize(f.b64).then(s => [f.id, s])))
      .then(entries => setFurniturePxSizes(Object.fromEntries(entries)))
  }, [furnitureList])

    const handleDelete = (instanceId) => {
    setPlacedMeshes(prev => {
      const next = { ...prev }
      delete next[instanceId]
      return next
    })
  }

  const handleCopy = (instanceId) => {
    setPlacedMeshes(prev => {
      const original = prev[instanceId]
      if (!original) return prev
      const newId = `${Date.now()}`
      return {
        ...prev,
        [newId]: {
          ...original,
          position: { x: original.position.x + 0.5, z: original.position.z + 0.5 }
        }
      }
    })
  }


  const useProceduralMesh = (furniture) => {
    const def = findProceduralDef(furniture.name)
    if (!def) { toast.error(`"${furniture.name}"의 기본 모델이 없습니다.`); return }
    const processed = {
      type: 'procedural',
      name: furniture.name,
      parts: def.parts,
      halfH:  def.h / 2,
      halfFX: def.w / 2,
      halfFZ: def.d / 2,
    }
    setFurnitureMeshes(prev => ({ ...prev, [furniture.id]: processed }))
    toast.success(`"${furniture.name}" 기본 모델 적용! 드래그해서 배치하세요`)
  }

  const generate3DMesh = async (furniture) => {
    // 창문만 자동 기본 모델 (유리는 SAM3D로 표현 불가)
    if ((furniture.name || '').includes('창문')) {
      const def = findProceduralDef(furniture.name)
      if (def) { useProceduralMesh(furniture); return }
    }
    setGenerating(true)
    setGeneratingId(furniture.id)
    toast.success('SAM3D로 3D 메쉬 생성 중...')
    try {
      const imgBlob = await fetch(`data:image/png;base64,${furniture.b64}`).then(r => r.blob())
      const imgFile = new File([imgBlob], 'furniture.png', { type: 'image/png' })
      const form = new FormData()
      form.append('image', imgFile)
      const res = await fetch('http://127.0.0.1:8001/api/sam3d/mesh', { method: 'POST', body: form })
      const data = await res.json()
      if (!data.success) throw new Error(data.error || '3D 생성 실패')
      // API 응답 시점에 typed array로 변환 — 이후 배치할 때마다 .flat() 재연산 없음
      const raw = data.mesh
      const processed = data.type === 'textured'
        ? {
            type: 'textured',
            vertices:   new Float32Array(raw.vertices.flat()),
            faces:      new Uint32Array(raw.faces.flat()),
            uvs:        new Float32Array(raw.uvs.flat()),
            textureB64: raw.texture,
          }
        : {
            type: 'vertex_color',
            vertices: new Float32Array(raw.vertices.flat()),
            faces:    new Uint32Array(raw.faces.flat()),
            colors:   new Float32Array(raw.colors.flat()),
          }
      setFurnitureMeshes(prev => ({ ...prev, [furniture.id]: { ...processed, name: furniture.name } }))
      toast.success('3D 변환 완료! 드래그해서 방에 배치하세요 🎉')
    } catch (e) {
      toast.error(`3D 생성 실패: ${e.message}`)
    } finally {
      setGenerating(false)
      setGeneratingId(null)
    }
  }

  const handleDrop = (furnitureId, position) => {
    const meshData = furnitureMeshes[furnitureId]
    if (!meshData) { toast.error('먼저 3D 변환을 해주세요!'); return }

    // 모든 가구 픽셀 크기 중 최대값 → 상대 크기 기준
    const allSizes = Object.values(furniturePxSizes)
    const maxPxDim = allSizes.length
      ? Math.max(...allSizes.map(s => Math.max(s.w, s.h)))
      : 100
    const thisPxDim = furniturePxSizes[furnitureId]
      ? Math.max(furniturePxSizes[furnitureId].w, furniturePxSizes[furnitureId].h)
      : maxPxDim
    // 가장 큰 가구 = 방 폭의 40%, 나머지는 비례
    const referenceRealSize = roomSize.width * 0.4
    const estimatedRealSize = referenceRealSize * (thisPxDim / maxPxDim)

    const instanceId = `${furnitureId}_${Date.now()}`
    setPlacedMeshes(prev => ({
      ...prev,
      [instanceId]: { data: meshData, position, estimatedRealSize }
    }))
    toast.success('가구 배치 완료! 🎉')
  }

  return (
    <div style={{
  position: 'relative',
  width: '100vw',
  marginLeft: 'calc(50% - 50vw)',
  height: 'calc(100vh - 110px)',  // 110px → 스텝바+헤더 높이 맞게 조정
  overflow: 'hidden',             // ← 이게 가로 스크롤 막아줌
  background: '#000000'
}}>

      {/* 3D 뷰어 - 전체 배경 */}
      <div id="room-viewer-wrap" style={{ position: 'absolute', inset: 0 }}>
        <RoomViewer
          roomSize={roomSize}
          roomColors={roomColorsMemo}
          roomTextures={roomTextures}
          placedMeshes={placedMeshes}
          onDrop={handleDrop}
          onDelete={handleDelete}
          onCopy={handleCopy}
          viewMode={viewMode}
        />
      </div>

      {/* 상단 타이틀 바 */}
      <div style={{
        position: 'absolute', top: 0, left: 0, right: 0, zIndex: 10,
        background: 'linear-gradient(to bottom, rgba(0,0,0,0.75), transparent)',
        padding: '16px 24px', display: 'flex', alignItems: 'center', gap: '16px'
      }}>
        <h2 style={{ margin: 0, fontSize: '18px', fontWeight: 700 }}>3D 인테리어 배치</h2>
        <span style={{ color: '#aaa', fontSize: '13px' }}>
          방 크기: {roomSize.width}m × {roomSize.depth}m × {roomSize.height}m
        </span>
        <span style={{ color: '#aaa', fontSize: '13px' }}>
          변환 {Object.keys(furnitureMeshes).length}개 · 배치 {Object.keys(placedMeshes).length}개
        </span>
        {/* 2D / 3D 토글 */}
        <div style={{ marginLeft: 'auto', display: 'flex', background: 'rgba(0,0,0,0.5)', borderRadius: '20px', padding: '3px' }}>
          {['2D','3D'].map(m => (
            <button key={m} onClick={() => setViewMode(m.toLowerCase())}
              style={{
                padding: '5px 16px', border: 'none', borderRadius: '16px', cursor: 'pointer',
                fontWeight: 700, fontSize: '13px', transition: 'all 0.2s',
                background: viewMode === m.toLowerCase() ? '#27ae60' : 'transparent',
                color: viewMode === m.toLowerCase() ? '#fff' : '#aaa',
              }}>{m}</button>
          ))}
        </div>
      </div>

      {/* 상단 중앙 안내 메시지 */}
      <div style={{
        position: 'absolute', top: '60px', left: '50%', transform: 'translateX(-50%)',
        zIndex: 10, background: 'rgba(0,0,0,0.5)', backdropFilter: 'blur(8px)',
        padding: '6px 16px', borderRadius: '20px',
        fontSize: '11px', color: '#ccc', whiteSpace: 'nowrap',
      }}>
        ▪ 왼쪽 가구를 드래그해서 방에 놓으세요 · 배치된 가구 좌클릭으로 이동 · 드래그로 카메라 회전
      </div>

      {/* 왼쪽 가구 패널 */}
      <div style={{
        position: 'absolute', top: '60px', left: '16px', bottom: '16px',
        width: '200px', zIndex: 10,
        background: 'rgba(20,20,20,0.85)', backdropFilter: 'blur(12px)',
        borderRadius: '16px', border: '1px solid rgba(255,255,255,0.08)',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        <div style={{ padding: '14px 14px 8px', borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
          <h3 style={{ margin: 0, fontSize: '13px', color: '#ddd', fontWeight: 600 }}>추출된 가구</h3>
        </div>
        <div style={{ flex: 1, overflowY: 'auto', padding: '10px' }}>
          {furnitureList.map((f, i) => (
            <div
              key={i}
              draggable={!!furnitureMeshes[f.id]}
              onDragStart={(e) => {
                if (furnitureMeshes[f.id]) {
                  e.dataTransfer.setData('furnitureId', String(f.id))
                  e.dataTransfer.setData('furnitureName', f.name || '')
                }
              }}
              style={{
                background: 'rgba(255,255,255,0.05)',
                borderRadius: '10px', padding: '8px', marginBottom: '10px',
                border: furnitureMeshes[f.id] ? '1.5px solid #27ae60' : '1.5px solid rgba(255,255,255,0.07)',
                cursor: furnitureMeshes[f.id] ? 'grab' : 'default',
                transition: 'border-color 0.2s',
              }}
            >
              {furnitureMeshes[f.id] ? (
                <MiniMeshViewer data={furnitureMeshes[f.id]} />
              ) : (
                <img src={`data:image/png;base64,${f.b64}`} style={{ width: '100%', borderRadius: '6px' }} />
              )}
              <p style={{ fontSize: '11px', color: '#bbb', margin: '6px 0 4px', textAlign: 'center' }}>
                {f.name || `가구 ${i + 1}`}
              </p>
              {furnitureMeshes[f.id] && (
                <p style={{ fontSize: '10px', color: '#27ae60', textAlign: 'center', marginBottom: '4px' }}>
                  ✅ 드래그해서 배치
                </p>
              )}
              <button
                onClick={() => generate3DMesh(f)}
                disabled={generating}
                style={{
                  width: '100%', padding: '6px', marginTop: '2px',
                  background: generating && generatingId === f.id ? '#444'
                    : furnitureMeshes[f.id] ? '#27ae60' : '#3498db',
                  color: 'white', border: 'none', borderRadius: '6px',
                  cursor: generating ? 'not-allowed' : 'pointer',
                  fontSize: '11px', fontWeight: 600,
                }}
              >
                {generating && generatingId === f.id ? '생성 중...' : furnitureMeshes[f.id] ? '🔄 재생성' : '🔷 3D 변환'}
              </button>
              {findProceduralDef(f.name) && (
                <button
                  onClick={() => useProceduralMesh(f)}
                  disabled={generating}
                  style={{
                    width: '100%', padding: '5px', marginTop: '4px',
                    background: 'rgba(155,89,182,0.7)', color: 'white',
                    border: '1px solid rgba(155,89,182,0.9)',
                    borderRadius: '6px', cursor: 'pointer', fontSize: '10px', fontWeight: 600,
                  }}
                >
                  📐 기본 모델 사용
                </button>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* 오른쪽 하단 컨트롤 버튼 */}
      <div style={{
        position: 'absolute', bottom: '24px', right: '24px', zIndex: 10,
        display: 'flex', flexDirection: 'column', gap: '8px',
      }}>
        {/* Undo / Redo */}
        <div style={{ display: 'flex', gap: '6px' }}>
          <button onClick={undo} disabled={!canUndo} style={{
            flex: 1, padding: '10px 12px', borderRadius: '10px', border: 'none',
            background: canUndo ? 'rgba(52,152,219,0.85)' : 'rgba(30,30,30,0.5)',
            backdropFilter: 'blur(8px)', color: 'white', fontWeight: 700, fontSize: '13px',
            cursor: canUndo ? 'pointer' : 'not-allowed',
          }}>↩ 취소</button>
          <button onClick={redo} disabled={!canRedo} style={{
            flex: 1, padding: '10px 12px', borderRadius: '10px', border: 'none',
            background: canRedo ? 'rgba(52,152,219,0.85)' : 'rgba(30,30,30,0.5)',
            backdropFilter: 'blur(8px)', color: 'white', fontWeight: 700, fontSize: '13px',
            cursor: canRedo ? 'pointer' : 'not-allowed',
          }}>↪ 다시</button>
        </div>
        <button onClick={handleSave} style={{
          padding: '10px 16px', background: 'rgba(39,174,96,0.85)', backdropFilter: 'blur(8px)',
          color: 'white', border: 'none', borderRadius: '10px', cursor: 'pointer',
          fontSize: '12px', fontWeight: 600,
        }}>💾 디자인 저장</button>
        <button onClick={() => setPlacedMeshes({})} style={{
          padding: '10px 16px', background: 'rgba(30,30,30,0.85)', backdropFilter: 'blur(8px)',
          color: '#ddd', border: '1px solid rgba(255,255,255,0.1)',
          borderRadius: '10px', cursor: 'pointer', fontSize: '12px', fontWeight: 600,
        }}>🗑️ 배치 초기화</button>
        <button onClick={reset} style={{
          padding: '10px 16px', background: 'rgba(231,76,60,0.85)', backdropFilter: 'blur(8px)',
          color: 'white', border: 'none', borderRadius: '10px', cursor: 'pointer',
          fontSize: '12px', fontWeight: 600,
        }}>🔄 처음부터</button>
      </div>

    </div>
  )
}