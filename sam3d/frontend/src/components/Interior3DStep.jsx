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
      const flippedVerts = data.vertices.slice()
      for (let i = 1; i < flippedVerts.length; i += 3) flippedVerts[i] = -flippedVerts[i]
      geometry.setAttribute('position', new THREE.BufferAttribute(flippedVerts, 3))
      geometry.setIndex(new THREE.BufferAttribute(data.faces.slice(), 1))
 
      let material
      if (data.type === 'textured') {
        geometry.setAttribute('uv', new THREE.BufferAttribute(data.uvs, 2))
        const texture = new THREE.TextureLoader().load(`data:image/jpeg;base64,${data.textureB64}`)
        texture.flipY = true
        material = new THREE.MeshBasicMaterial({ map: texture, side: THREE.DoubleSide })
      } else {
        geometry.setAttribute('color', new THREE.BufferAttribute(data.colors.slice(), 3))
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
 
function RoomViewer({ roomSize, roomColors, roomTextures, roomSurfaceTextures, roomBoxTextures, roomMesh, placedMeshes, onDrop, onDelete, onCopy, viewMode }) {
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
      // 가로 나무 판자 (달하우스 스타일 나무바닥)
      const plankH = 36
      for (let y = 0; y <= 512; y += plankH) {
        // 판자 경계선
        ctx.strokeStyle = 'rgba(0,0,0,0.12)'
        ctx.lineWidth = 1.5
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(512, y); ctx.stroke()
        // 판자 중간 결 (밝게)
        ctx.strokeStyle = 'rgba(255,255,255,0.06)'
        ctx.lineWidth = 0.8
        ctx.beginPath(); ctx.moveTo(0, y + plankH * 0.4); ctx.lineTo(512, y + plankH * 0.4); ctx.stroke()
        // 판자 이음새 (엇갈리게)
        const offset = (Math.floor(y / plankH) % 2 === 0) ? 180 : 360
        ctx.strokeStyle = 'rgba(0,0,0,0.08)'
        ctx.lineWidth = 1
        ctx.beginPath(); ctx.moveTo(offset, y); ctx.lineTo(offset, y + plankH); ctx.stroke()
      }
    }
    const texture = new window.THREE.CanvasTexture(canvas)
    texture.wrapS = texture.wrapT = window.THREE.RepeatWrapping
    texture.repeat.set(3, 3)
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
  const ceilingRef = useRef(null)
  const viewModeRef = useRef('3d')
  const camTargetRef = useRef({ x: 0, y: 0, z: 0 })
  const [contextMenu, setContextMenu] = useState(null)
 
  useEffect(() => {
    if (!mountRef.current) return
    const THREE = window.THREE
    const el = mountRef.current
    const getSize = () => ({ w: el.offsetWidth || window.innerWidth, h: el.offsetHeight || window.innerHeight - 110 })
    const { w: width, h: height } = getSize()
 
    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x1a1a2e)
    sceneRef.current = scene
 
    const camera = new THREE.PerspectiveCamera(35, width / height, 0.01, 1000)
    const _cd = Math.max(roomSize.width, roomSize.depth, roomSize.height) * 2.2
    camera.position.set(_cd * 0.8, _cd * 0.6, _cd * 0.8)
    camera.lookAt(0, 0, 0)
    cameraRef.current = camera
 
    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true })
    renderer.setSize(width, height)
    renderer.shadowMap.enabled = false
    renderer.outputColorSpace = THREE.SRGBColorSpace
    el.appendChild(renderer.domElement)
    rendererRef.current = renderer
 
    const onResize = () => {
      const { w, h } = getSize()
      camera.aspect = w / h
      camera.updateProjectionMatrix()
      renderer.setSize(w, h)
    }
    const resizeObserver = new ResizeObserver(onResize)
    resizeObserver.observe(el)
    const raycaster = new THREE.Raycaster()
 
    // 달하우스 스타일 조명
    scene.add(new THREE.AmbientLight(0xffffff, 0.75))
    const keyLight = new THREE.DirectionalLight(0xffffff, 0.85)
    keyLight.position.set(8, 12, 8)
    scene.add(keyLight)
    const fillLight = new THREE.DirectionalLight(0xffffff, 0.3)
    fillLight.position.set(-6, 4, -6)
    scene.add(fillLight)
    const rimLight = new THREE.DirectionalLight(0xffffff, 0.15)
    rimLight.position.set(-4, 8, 4)
    scene.add(rimLight)
 
    const w = roomSize.width
    const h = roomSize.height
    const d = roomSize.depth
    const wc = roomColors.wall
    const fc = roomColors.floor
    const fColor = `rgb(${fc[0]*255}, ${fc[1]*255}, ${fc[2]*255})`
 
    const loader = new THREE.TextureLoader()
    const makeSurfaceMat = (b64, fallbackColor, roughness = 0.8) => {
      if (b64) {
        const tex = loader.load(`data:image/jpeg;base64,${b64}`)
        tex.wrapS = tex.wrapT = THREE.RepeatWrapping
        return new THREE.MeshStandardMaterial({ map: tex, roughness, side: THREE.DoubleSide })
      }
      return new THREE.MeshStandardMaterial({ color: new THREE.Color(...fallbackColor), roughness, side: THREE.DoubleSide })
    }
 
    if (roomMesh) {
      // ── 1. geometry 구성 (vertex color 원본 그대로) ──
      const geo = new THREE.BufferGeometry()
      const nv = roomMesh.vertices.length
      const verts = new Float32Array(nv * 3)
      for (let i = 0; i < nv; i++) {
        verts[i*3]     =  roomMesh.vertices[i][0]
        verts[i*3 + 1] = -roomMesh.vertices[i][1]   // Y축 반전: PyTorch3D → Three.js
        verts[i*3 + 2] =  roomMesh.vertices[i][2]
      }
      const nf = roomMesh.faces.length
      const faces = new Uint32Array(nf * 3)
      for (let i = 0; i < nf; i++) {
        faces[i*3]     = roomMesh.faces[i][0]
        faces[i*3 + 1] = roomMesh.faces[i][1]
        faces[i*3 + 2] = roomMesh.faces[i][2]
      }
      // 서버 색상 그대로 사용 (재색칠 없음)
      const rawColors = roomMesh.colors
      const cols = new Float32Array(nv * 3)
      for (let i = 0; i < nv; i++) {
        cols[i*3]   = rawColors[i][0]
        cols[i*3+1] = rawColors[i][1]
        cols[i*3+2] = rawColors[i][2]
      }
 
      geo.setAttribute('position', new THREE.BufferAttribute(verts, 3))
      geo.setIndex(new THREE.BufferAttribute(faces, 1))
      geo.setAttribute('color', new THREE.BufferAttribute(cols, 3))
      geo.computeVertexNormals()
      geo.computeBoundingBox()
 
      // ── 2. mesh 생성 + 스케일/위치 ──
      const center = new THREE.Vector3()
      geo.boundingBox.getCenter(center)
      const mSize = new THREE.Vector3()
      geo.boundingBox.getSize(mSize)
      const sx = w / mSize.x
      const sz = d / mSize.z
 
      console.log('mesh size:', mSize, 'sx:', sx.toFixed(3), 'sz:', sz.toFixed(3), 'room w/d:', w, d)
 
      const mat = new THREE.MeshStandardMaterial({ vertexColors: true, side: THREE.DoubleSide, roughness: 0.85, metalness: 0 })
      const roomMeshObj = new THREE.Mesh(geo, mat)
      roomMeshObj.scale.set(sx, sz, sz)
 
      const meshFloorY = (geo.boundingBox.min.y - center.y) * sz
      const yShift = -h / 2 - meshFloorY
      roomMeshObj.position.set(-center.x * sx, -center.y * sz + yShift, -center.z * sz)
 
      // ── 3. 씬에 추가 ──
      scene.add(roomMeshObj)
 
      // ── 4. 실제 world bounding box 계산 (스케일/위치 반영 후) ──
      const box = new THREE.Box3().setFromObject(roomMeshObj)
      const bSize = new THREE.Vector3(); box.getSize(bSize)
      const bCenter = new THREE.Vector3(); box.getCenter(bCenter)
 
      // ── 5. raycasting용 invisible planes ──
      const invisMat = new THREE.MeshBasicMaterial({ visible: false, side: THREE.DoubleSide })
 
      const floor = new THREE.Mesh(new THREE.PlaneGeometry(bSize.x * 2, bSize.z * 2), invisMat)
      floor.rotation.x = -Math.PI / 2
      floor.position.set(bCenter.x, box.min.y, bCenter.z)
      floor.name = 'floor'
      scene.add(floor); floorRef.current = floor
 
      const backWall = new THREE.Mesh(new THREE.PlaneGeometry(bSize.x * 2, bSize.y * 2), invisMat)
      backWall.position.set(bCenter.x, bCenter.y, box.min.z)
      backWall.name = 'wall_back'
      scene.add(backWall); backWallRef.current = backWall
 
      const leftWall = new THREE.Mesh(new THREE.PlaneGeometry(bSize.z * 2, bSize.y * 2), invisMat)
      leftWall.rotation.y = Math.PI / 2
      leftWall.position.set(box.min.x, bCenter.y, bCenter.z)
      leftWall.name = 'wall_left'
      scene.add(leftWall); leftWallRef.current = leftWall
 
      const rightWall = new THREE.Mesh(new THREE.PlaneGeometry(bSize.z * 2, bSize.y * 2), invisMat)
      rightWall.rotation.y = -Math.PI / 2
      rightWall.position.set(box.max.x, bCenter.y, bCenter.z)
      rightWall.name = 'wall_right'
      scene.add(rightWall); rightWallRef.current = rightWall
 
      console.log('[Room planes] floor Y:', box.min.y.toFixed(2), 'back Z:', box.min.z.toFixed(2),
                  'left X:', box.min.x.toFixed(2), 'right X:', box.max.x.toFixed(2),
                  'center:', bCenter.x.toFixed(2), bCenter.y.toFixed(2), bCenter.z.toFixed(2))
 
    } else {
      // ── 인테리어 시뮬레이션 박스 방 ──
      const WT = 0.08   // 벽 두께
      const FT = 0.04   // 바닥 두께
      const BH = 0.10   // 걸레받이 높이
      const BD = 0.025  // 걸레받이 두께

      // A-3: mipmap + anisotropic filtering — 원거리 벽/바닥이 뭉개지거나 aliasing 나는 것 완화
      const maxAniso = renderer.capabilities.getMaxAnisotropy()
      const applyFiltering = (tex) => {
        tex.generateMipmaps = true
        tex.minFilter = THREE.LinearMipmapLinearFilter
        tex.magFilter = THREE.LinearFilter
        tex.anisotropy = maxAniso
        return tex
      }

      // uLayout rectify 실사 텍스처(있으면 사용) — 이미 해당 면 전체 크기로 펴져 있으므로 타일링 없이 1:1 매핑
      const makeRectifiedMat = (b64, side = THREE.FrontSide) => {
        if (!b64) return null
        const tex = applyFiltering(loader.load(`data:image/jpeg;base64,${b64}`))
        tex.wrapS = tex.wrapT = THREE.ClampToEdgeWrapping
        return new THREE.MeshStandardMaterial({ map: tex, roughness: 0.9, metalness: 0, side })
      }
      const rectBack  = roomBoxTextures && makeRectifiedMat(roomBoxTextures.back_wall, THREE.DoubleSide)
      const rectLeft  = roomBoxTextures && makeRectifiedMat(roomBoxTextures.left_wall)
      const rectFloor = roomBoxTextures && makeRectifiedMat(roomBoxTextures.floor)

      // 원본 사진에서 오려낸 "무늬 균일한" 패치를 타일링하는 폴백 (RANSAC 코너 검출 실패 시에도
      // 단색 대신 사진 질감이 살아있도록 함). rectify 실사 텍스처가 있으면 그게 우선.
      const makeTiledPatchMat = (b64, repeatX, repeatY, side = THREE.FrontSide) => {
        if (!b64) return null
        const tex = applyFiltering(loader.load(`data:image/jpeg;base64,${b64}`))
        tex.wrapS = tex.wrapT = THREE.RepeatWrapping
        tex.repeat.set(repeatX, repeatY)
        return new THREE.MeshStandardMaterial({ map: tex, roughness: 0.9, metalness: 0, side })
      }
      const tileWall  = roomSurfaceTextures?.wall  && makeTiledPatchMat(roomSurfaceTextures.wall, 4, 3)
      const tileBack  = roomSurfaceTextures?.wall  && makeTiledPatchMat(roomSurfaceTextures.wall, 4, 3, THREE.DoubleSide)
      const tileFloor = roomSurfaceTextures?.floor && makeTiledPatchMat(roomSurfaceTextures.floor, 5, 5)

      // 벽: 1순위 rectify 실사, 2순위 사진 패치 타일링, 3순위(둘 다 없으면) 추출 색상 단색
      const wallMat = rectLeft || tileWall || new THREE.MeshStandardMaterial({
        color: new THREE.Color(...wc), roughness: 0.92, metalness: 0, side: THREE.FrontSide
      })
      // 바닥: 1순위 rectify 실사, 2순위 사진 패치 타일링, 3순위 절차적 나무 패턴
      const floorMat = rectFloor || tileFloor || new THREE.MeshStandardMaterial({
        map: createDynamicTexture(fColor, 'plank'), roughness: 0.75, metalness: 0
      })
      // 걸레받이: 벽보다 약간 어두운 단색
      const baseColor = new THREE.Color(...wc).multiplyScalar(0.7)
      const baseMat = new THREE.MeshStandardMaterial({ color: baseColor, roughness: 0.8 })

      // 바닥 (두께 있는 박스)
      const floor = new THREE.Mesh(new THREE.BoxGeometry(w, FT, d), floorMat)
      floor.position.y = -h / 2 - FT / 2; floor.name = 'floor'
      scene.add(floor); floorRef.current = floor

      // 뒷벽
      const backWallMat = rectBack || tileBack || new THREE.MeshStandardMaterial({ color: new THREE.Color(...wc), roughness: 0.92, metalness: 0, side: THREE.DoubleSide })
      const backWall = new THREE.Mesh(new THREE.BoxGeometry(w, h, WT), backWallMat)
      backWall.position.set(0, 0, -d / 2 - WT / 2); backWall.name = 'wall_back'
      scene.add(backWall); backWallRef.current = backWall

      // 왼쪽 벽
      const leftWall = new THREE.Mesh(new THREE.BoxGeometry(WT, h, d), wallMat)
      leftWall.position.set(-w / 2 - WT / 2, 0, 0); leftWall.name = 'wall_left'
      scene.add(leftWall); leftWallRef.current = leftWall

      // 걸레받이 (뒷벽, 왼쪽)
      const bBack  = new THREE.Mesh(new THREE.BoxGeometry(w, BH, BD), baseMat)
      bBack.position.set(0, -h / 2 + BH / 2, -d / 2 + BD / 2)
      scene.add(bBack)
      const bLeft  = new THREE.Mesh(new THREE.BoxGeometry(BD, BH, d), baseMat)
      bLeft.position.set(-w / 2 + BD / 2, -h / 2 + BH / 2, 0)
      scene.add(bLeft)

      // raycasting용 invisible plane (바닥은 실제 박스 위쪽 면 높이)
      const invisMat2 = new THREE.MeshBasicMaterial({ visible: false, side: THREE.DoubleSide })

      // 천장 — 보이지 않지만 조명 배치용 raycasting plane
      const ceilingPlane = new THREE.Mesh(new THREE.PlaneGeometry(w, d), invisMat2)
      ceilingPlane.rotation.x = Math.PI / 2
      ceilingPlane.position.y = h / 2
      ceilingPlane.name = 'ceiling'
      scene.add(ceilingPlane); ceilingRef.current = ceilingPlane
      const floorPlane = new THREE.Mesh(new THREE.PlaneGeometry(w, d), invisMat2)
      floorPlane.rotation.x = -Math.PI / 2
      floorPlane.position.y = -h / 2
      floorPlane.name = 'floor'
      scene.add(floorPlane); floorRef.current = floorPlane

      const bwPlane = new THREE.Mesh(new THREE.PlaneGeometry(w, h), invisMat2)
      bwPlane.position.z = -d / 2; bwPlane.name = 'wall_back'
      scene.add(bwPlane); backWallRef.current = bwPlane

      const lwPlane = new THREE.Mesh(new THREE.PlaneGeometry(d, h), invisMat2)
      lwPlane.rotation.y = Math.PI / 2; lwPlane.position.x = -w / 2; lwPlane.name = 'wall_left'
      scene.add(lwPlane); leftWallRef.current = lwPlane

      rightWallRef.current = null
    }
 
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
    let mouseDownX = 0, mouseDownY = 0
 
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
 
        const sel = selectedObjRef.current
        const prevX = sel.position.x
        const prevZ = sel.position.z

        // 다른 가구 위에 올리기 — 위쪽 면(normal.y > 0.5) 감지
        const others = Object.values(placedGroupsRef.current).filter(
          g => g !== sel && !g.userData.isWallItem && !g.userData.isCeilingItem
        )
        const furHits = raycaster.intersectObjects(others, true)
        const topHit = furHits.find(h => {
          const n = h.face?.normal.clone().applyQuaternion(h.object.getWorldQuaternion(new THREE.Quaternion()))
          return n && n.y > 0.5
        })

        if (topHit) {
          sel.position.x = topHit.point.x
          sel.position.z = topHit.point.z
          sel.position.y = topHit.point.y + (sel.userData.halfSize || 0.3)
        } else {
          const floorHits = floorRef.current ? raycaster.intersectObject(floorRef.current) : []
          if (floorHits.length > 0) {
            const halfW = roomSize.width / 2 - 0.02
            const halfD = roomSize.depth / 2 - 0.02
            sel.position.x = Math.max(-halfW, Math.min(halfW, floorHits[0].point.x))
            sel.position.z = Math.max(-halfD, Math.min(halfD, floorHits[0].point.z))

            // 바닥 레벨에서만 XZ 충돌 체크
            const shx = (sel.userData.halfFX || sel.userData.halfSize || 0.3) - 0.04
            const shz = (sel.userData.halfFZ || sel.userData.halfSize || 0.3) - 0.04
            const shy = (sel.userData.halfSize || 0.3) - 0.04
            const sp = sel.position
            const collides = others.some(g => {
              const ghx = (g.userData.halfFX || g.userData.halfSize || 0.3) - 0.04
              const ghz = (g.userData.halfFZ || g.userData.halfSize || 0.3) - 0.04
              const ghy = (g.userData.halfSize || 0.3) - 0.04
              const gp = g.position
              const xzOvlp = sp.x - shx < gp.x + ghx && sp.x + shx > gp.x - ghx &&
                             sp.z - shz < gp.z + ghz && sp.z + shz > gp.z - ghz
              const yOvlp  = sp.y - shy < gp.y + ghy && sp.y + shy > gp.y - ghy
              return xzOvlp && yOvlp
            })
            if (collides) {
              sel.position.x = prevX
              sel.position.z = prevZ
            }
          }
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
    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onMouseUp)
    renderer.domElement.addEventListener('wheel', onWheel, { passive: false })
 
    let animId
    const animate = () => { animId = requestAnimationFrame(animate); renderer.render(scene, camera) }
    animate()
 
    return () => {
      cancelAnimationFrame(animId)
      renderer.domElement.removeEventListener('wheel', onWheel)
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('mouseup', onMouseUp)
      resizeObserver.disconnect()
      renderer.dispose()
      if (el.contains(renderer.domElement)) el.removeChild(renderer.domElement)
    }
  }, [roomSize, roomColors, roomTextures, roomBoxTextures])
 
  useEffect(() => {
    if (!sceneRef.current) return
    const THREE = window.THREE
 
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
        if (position.isCeiling) {
          group.position.set(position.x, roomSize.height / 2 - data.halfH, position.z)
          group.userData.isCeilingItem = true
          group.userData.halfSize = 0
        } else if (position.wallNormal) {
          const wn = position.wallNormal
          const backOffset = roomSize.depth * 0.04
          const sideOffset = roomSize.width * 0.04
          group.position.set(
            wn === 'left'  ? position.x + sideOffset
              : wn === 'right' ? position.x - sideOffset : position.x,
            position.y,
            wn === 'back'  ? position.z + backOffset : position.z
          )
          group.rotation.y = wn === 'left' ? Math.PI / 2 : wn === 'right' ? -Math.PI / 2 : 0
          group.userData.isWallItem = true
          group.userData.wallNormal = wn
          group.userData.halfSize = 0
        } else if (position.onFurniture) {
          group.position.set(position.x, position.y + data.halfH, position.z)
          group.userData.halfSize = data.halfH
        } else {
          group.position.set(position.x, -roomSize.height / 2 + data.halfH, position.z)
          group.userData.halfSize = data.halfH
        }
        group.userData.halfFX = data.halfFX
        group.userData.halfFZ = data.halfFZ
      } else {
        const geometry = new THREE.BufferGeometry()
        const flippedVerts = data.vertices.slice()
        for (let i = 1; i < flippedVerts.length; i += 3) flippedVerts[i] = -flippedVerts[i]
        geometry.setAttribute('position', new THREE.BufferAttribute(flippedVerts, 3))
        geometry.setIndex(new THREE.BufferAttribute(data.faces.slice(), 1))
 
        let material
        if (data.type === 'textured') {
          geometry.setAttribute('uv', new THREE.BufferAttribute(data.uvs, 2))
          const texture = new THREE.TextureLoader().load(`data:image/jpeg;base64,${data.textureB64}`)
          texture.flipY = true
          material = new THREE.MeshBasicMaterial({ map: texture, side: THREE.DoubleSide })
        } else {
          geometry.setAttribute('color', new THREE.BufferAttribute(data.colors.slice(), 3))
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
          const stdMaxDim = Math.max(std.w, std.d, std.h)
          const s = stdMaxDim / maxDim
          scaleX = s; scaleY = s; scaleZ = s
          scaledHalfHeight = (size.y * s) / 2
        } else if (std && data.type === 'textured') {
          scaleX = size.x > 0 ? std.w / size.x : 1
          scaleY = size.y > 0 ? std.h / size.y : 1
          scaleZ = size.z > 0 ? std.d / size.z : 1
          scaledHalfHeight = std.h / 2
        } else {
          // estimatedRealSize는 이제 "추정 실제 높이"(카메라 pose 기반, handleDrop에서 계산)이므로
          // 메쉬의 raw 높이(size.y)에 맞춰 스케일 — 이전엔 maxDim 기준이라 가구가 세로보다
          // 가로로 긴 경우(소파 등) 실제 높이와 안 맞았음.
          const targetHeight = estimatedRealSize || roomSize.width * 0.2
          const s = size.y > 0 ? targetHeight / size.y : targetHeight / maxDim
          scaleX = s; scaleY = s; scaleZ = s
          scaledHalfHeight = (size.y * s) / 2
        }
 
        const mesh = new THREE.Mesh(geometry, material)
        mesh.castShadow = true
        mesh.receiveShadow = true
        mesh.scale.set(scaleX, scaleY, scaleZ)
 
        const halfFX = (size.x * scaleX) / 2
        const halfFZ = (size.z * scaleZ) / 2
 
        group = new THREE.Group()
        group.add(mesh)
 
        if (position.isCeiling) {
          group.position.set(position.x, roomSize.height / 2 - scaledHalfHeight, position.z)
          group.userData.isCeilingItem = true
          group.userData.halfSize = 0
        } else if (position.wallNormal) {
          const wn = position.wallNormal
          const backOffset = roomSize.depth * 0.04
          const sideOffset = roomSize.width * 0.04
          group.position.set(
            wn === 'left'  ? position.x + sideOffset
              : wn === 'right' ? position.x - sideOffset : position.x,
            position.y,
            wn === 'back'  ? position.z + backOffset : position.z
          )
          group.rotation.y = wn === 'left' ? Math.PI / 2 : wn === 'right' ? -Math.PI / 2 : 0
          group.userData.isWallItem = true
          group.userData.wallNormal = wn
          group.userData.halfSize = 0
        } else if (position.onFurniture) {
          group.position.set(position.x, position.y + scaledHalfHeight, position.z)
          group.userData.halfSize = scaledHalfHeight
        } else {
          group.position.set(position.x, -roomSize.height / 2 + scaledHalfHeight, position.z)
          group.userData.halfSize = scaledHalfHeight
        }
        group.userData.halfFX = halfFX
        group.userData.halfFZ = halfFZ
      }
 
      sceneRef.current.add(group)
      placedGroupsRef.current[instanceId] = group

      if (!group.userData.isWallItem && !group.userData.isCeilingItem) {
        const box = new THREE.Box3().setFromObject(group)
        const floorY = -roomSize.height / 2
        const bottomY = box.min.y
        if (Math.abs(bottomY - floorY) > 0.02) {
          group.position.y += floorY - bottomY
        }
      }
    })
  }, [placedMeshes])
 
  useEffect(() => {
    viewModeRef.current = viewMode
    if (!cameraRef.current) return
    if (viewMode === '2d') {
      const h = Math.max(roomSize.width, roomSize.depth) * 1.5
      cameraRef.current.position.set(camTargetRef.current.x, h, camTargetRef.current.z + 0.001)
      cameraRef.current.lookAt(camTargetRef.current.x, 0, camTargetRef.current.z)
    } else {
      camTargetRef.current = { x: 0, y: 0, z: 0 }
      const cd = Math.max(roomSize.width, roomSize.depth, roomSize.height) * 2.2
      cameraRef.current.position.set(cd * 0.8, cd * 0.6, cd * 0.8)
      cameraRef.current.lookAt(0, 0, 0)
    }
  }, [viewMode])
 
  const handleDragOver = (e) => e.preventDefault()
 
  const handleDrop = (e) => {
    e.preventDefault()
    const furnitureId = e.dataTransfer.getData('furnitureId')
    const furnitureName = e.dataTransfer.getData('furnitureName') || ''
    const isWallItem = [...WALL_ITEM_NAMES].some(k => furnitureName.includes(k))
    // '스탠드 조명'은 바닥 아이템 — '조명' 포함 여부 체크 전에 제외
    const isCeilingItem = !furnitureName.includes('스탠드') && [...CEILING_ITEM_NAMES].some(k => furnitureName.includes(k))
    const isCeilingOrFloor = [...CEILING_OR_FLOOR_NAMES].some(k => furnitureName.includes(k))
    const THREE = window.THREE
    const raycaster = new THREE.Raycaster()
    const rect = mountRef.current.getBoundingClientRect()
    const nx = ((e.clientX - rect.left) / rect.width) * 2 - 1
    const ny = -((e.clientY - rect.top) / rect.height) * 2 + 1
    if (!cameraRef.current) return
    raycaster.setFromCamera({ x: nx, y: ny }, cameraRef.current)
 
    if (isCeilingItem) {
      if (ceilingRef.current) {
        const hits = raycaster.intersectObject(ceilingRef.current)
        if (hits.length > 0)
          onDrop(furnitureId, { x: hits[0].point.x, z: hits[0].point.z, isCeiling: true })
      }
    } else if (isCeilingOrFloor) {
      let placed = false
      if (ceilingRef.current) {
        const hits = raycaster.intersectObject(ceilingRef.current)
        if (hits.length > 0) {
          onDrop(furnitureId, { x: hits[0].point.x, z: hits[0].point.z, isCeiling: true })
          placed = true
        }
      }
      if (!placed && floorRef.current) {
        const hits = raycaster.intersectObject(floorRef.current)
        if (hits.length > 0)
          onDrop(furnitureId, { x: hits[0].point.x, z: hits[0].point.z })
      }
    } else if (isWallItem) {
      const walls = [backWallRef.current, leftWallRef.current, rightWallRef.current].filter(Boolean)
      const hits = raycaster.intersectObjects(walls)
      if (hits.length > 0) {
        const hit = hits[0]
        const name = hit.object.name
        const wallNormal = name === 'wall_back' ? 'back' : name === 'wall_left' ? 'left' : 'right'
        onDrop(furnitureId, { x: hit.point.x, y: hit.point.y, z: hit.point.z, wallNormal })
      } else if (floorRef.current) {
        const floorHits = raycaster.intersectObject(floorRef.current)
        if (floorHits.length > 0)
          onDrop(furnitureId, { x: floorHits[0].point.x, z: floorHits[0].point.z })
      }
    } else {
      const placedMeshObjects = Object.values(placedGroupsRef.current).filter(Boolean)
      let placed = false
      if (placedMeshObjects.length > 0) {
        const furnitureHits = raycaster.intersectObjects(placedMeshObjects, true)
        const topHit = furnitureHits.find(h => {
          const normal = h.face?.normal.clone().applyQuaternion(h.object.getWorldQuaternion(new THREE.Quaternion()))
          return normal && normal.y > 0.5
        })
        if (topHit) {
          onDrop(furnitureId, { x: topHit.point.x, y: topHit.point.y, z: topHit.point.z, onFurniture: true })
          placed = true
        }
      }
      if (!placed && floorRef.current) {
        const hits = raycaster.intersectObject(floorRef.current)
        if (hits.length > 0)
          onDrop(furnitureId, { x: hits[0].point.x, z: hits[0].point.z })
      }
    }
  }
 
  const handleRotate = () => {
    if (selectedObjRef.current) selectedObjRef.current.rotation.y += Math.PI / 2
    setContextMenu(null)
  }
 
  const handleRotateX = () => {
    const obj = selectedObjRef.current
    if (!obj) return
    obj.rotation.x += Math.PI / 2
    const THREE = window.THREE
    const box = new THREE.Box3().setFromObject(obj)
    const floorY = -roomSize.height / 2
    const bottomY = box.min.y
    if (bottomY < floorY) obj.position.y += floorY - bottomY
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
    if (contextMenu?.instanceId) onCopy(contextMenu.instanceId)
    setContextMenu(null)
  }
 
  const handleScale = (factor) => {
    if (selectedObjRef.current) {
      const group = selectedObjRef.current
      group.scale.multiplyScalar(factor)
      group.userData.halfSize = (group.userData.halfSize || 0.5) * factor
      group.userData.halfFX = (group.userData.halfFX || group.userData.halfSize) * factor
      group.userData.halfFZ = (group.userData.halfFZ || group.userData.halfSize) * factor
      if (!group.userData.isWallItem && !group.userData.isCeilingItem) {
        const T = window.THREE
        const box = new T.Box3().setFromObject(group)
        const floorY = -roomSize.height / 2
        group.position.y += floorY - box.min.y
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
          <button style={btnStyle('#3498db')} onClick={handleRotate} title="좌우 회전">🔄</button>
          <button style={btnStyle('#3498db')} onClick={handleRotateX} title="앞뒤 기울기">↕️</button>
          <button style={btnStyle('#27ae60')} onClick={handleCopy} title="복사">📋</button>
          <button style={btnStyle('#e67e22')} onClick={() => handleScale(1.2)} title="크게">＋</button>
          <button style={btnStyle('#e67e22')} onClick={() => handleScale(1/1.2)} title="작게">－</button>
          <button style={btnStyle('#e74c3c')} onClick={handleDelete} title="삭제">🗑️</button>
        </div>
      )}
    </div>
  )
}
 
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
 
function findStdSize(name) {
  if (!name) return null
  if (FURNITURE_STD_SIZES[name]) return FURNITURE_STD_SIZES[name]
  const sorted = Object.entries(FURNITURE_STD_SIZES).sort((a, b) => b[0].length - a[0].length)
  for (const [key, val] of sorted) {
    if (name.includes(key)) return val
  }
  return null
}

// 카메라 pose(uLayout solvePnP) + 가구의 원본 사진 픽셀 bbox로 가구의 실제 높이(m)를 추정.
// 원리: bbox 하단 중앙을 "가구가 바닥에 닿는 점"으로 보고, 카메라에서 그 픽셀 방향으로
// 광선을 쏴서 방 좌표계의 바닥평면(Y=0)과 만나는 지점을 구하면 그 지점까지의 실제 거리를
// 알 수 있다. 그 거리와 bbox의 세로 픽셀 길이를 핀홀 카메라 비례식에 넣으면 실제 높이가 나옴
// (원근 보정 포함 — 단순 픽셀 비율보다 정확, 카메라로부터의 거리를 직접 반영하기 때문).
// bbox는 extract.py가 원본(리사이즈 전) 사진 좌표계로 반환하는 (rmin,rmax,cmin,cmax) 포맷.
function estimateFurnitureHeightM(cameraPose, bbox) {
  if (!cameraPose || !bbox || bbox.length !== 4) return null
  const { rotation_matrix: R, translation: t, K } = cameraPose
  const fx = K[0][0], fy = K[1][1], cx = K[0][2], cy = K[1][2]
  const [y0, y1, x0, x1] = bbox
  const xCenter = (x0 + x1) / 2
  const yBottom = y1

  const matTMulVec = (M, v) => [
    M[0][0]*v[0] + M[1][0]*v[1] + M[2][0]*v[2],
    M[0][1]*v[0] + M[1][1]*v[1] + M[2][1]*v[2],
    M[0][2]*v[0] + M[1][2]*v[1] + M[2][2]*v[2],
  ]
  const matMulVec = (M, v) => [
    M[0][0]*v[0] + M[0][1]*v[1] + M[0][2]*v[2],
    M[1][0]*v[0] + M[1][1]*v[1] + M[1][2]*v[2],
    M[2][0]*v[0] + M[2][1]*v[1] + M[2][2]*v[2],
  ]

  const camPosRoom = matTMulVec(R, [-t[0], -t[1], -t[2]])  // -R^T t
  const dirCam = [(xCenter - cx) / fx, (yBottom - cy) / fy, 1.0]
  const dirRoom = matTMulVec(R, dirCam)

  if (Math.abs(dirRoom[1]) < 1e-6) return null   // 바닥과 거의 평행 -> 계산 불가
  const s = -camPosRoom[1] / dirRoom[1]
  if (s <= 0) return null   // 카메라 뒤쪽/무효

  const floorPoint = [
    camPosRoom[0] + s * dirRoom[0],
    0,
    camPosRoom[2] + s * dirRoom[2],
  ]
  const pCamFloor = matMulVec(R, floorPoint)
  const zCam = pCamFloor[2] + t[2]   // 카메라 광축 기준 깊이 (핀홀 스케일 공식용)
  if (zCam <= 0) return null

  const pixelHeight = yBottom - y0
  const realHeight = pixelHeight * zCam / fy
  if (!(realHeight > 0) || !isFinite(realHeight)) return null
  return realHeight
}

// 카메라 pose로부터 "방 바닥(Y=0) 기준 카메라의 실제 높이(m)"를 역산.
// precise(solvePnP)에서는 실측 대상, approx에서는 build_approx_pose가 가정한 값(기본 1.6m)이 그대로 나옴 — 검증용.
function estimateCameraHeightM(cameraPose) {
  if (!cameraPose) return null
  const { rotation_matrix: R, translation: t } = cameraPose
  return R[0][1] * -t[0] + R[1][1] * -t[1] + R[2][1] * -t[2]
}

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
    { size:[1.60,0.24,2.00], pos:[0,0.12, 0],        c:[0.58,0.48,0.38] },
    { size:[1.50,0.20,1.85], pos:[0,0.34, 0.05],     c:[0.96,0.94,0.91] },
    { size:[1.60,0.52,0.09], pos:[0,0.40,-0.955],    c:[0.52,0.42,0.32] },
    { shape:'pillow', size:[0.58,0.12,0.36], pos:[-0.34,0.47,-0.68], c:[0.97,0.96,0.94] },
    { shape:'pillow', size:[0.58,0.12,0.36], pos:[ 0.34,0.47,-0.68], c:[0.97,0.96,0.94] },
    { size:[1.48,0.11,1.15], pos:[0,0.43, 0.38],     c:[0.72,0.78,0.90] },
    { size:[1.46,0.06,0.30], pos:[0,0.50, 0.93],     c:[0.80,0.85,0.96] },
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
  '문': { w:0.9, d:0.06, h:2.1, wallItem:true, parts:[
    { size:[0.06,2.10,0.06], pos:[-0.42, 0,    0],    c:[0.52,0.38,0.26] },
    { size:[0.06,2.10,0.06], pos:[ 0.42, 0,    0],    c:[0.52,0.38,0.26] },
    { size:[0.90,0.06,0.06], pos:[ 0,    1.02, 0],    c:[0.52,0.38,0.26] },
    { size:[0.78,1.98,0.04], pos:[ 0,    0,    0.01], c:[0.78,0.62,0.45] },
    { size:[0.66,0.68,0.02], pos:[ 0,    0.56, 0.03], c:[0.84,0.70,0.52] },
    { size:[0.66,0.90,0.02], pos:[ 0,   -0.42, 0.03], c:[0.84,0.70,0.52] },
    { size:[0.05,0.12,0.05], pos:[ 0.30, 0.05, 0.06], c:[0.75,0.72,0.65] },
    { size:[0.06,0.03,0.03], pos:[ 0.36, 0.05, 0.06], c:[0.75,0.72,0.65] },
  ]},
  '창문': { w:1.2, d:0.10, h:1.0, wallItem:true, parts:[
    { size:[1.20,0.07,0.10], pos:[0, 0.465,0],  c:[0.90,0.90,0.90] },
    { size:[1.20,0.07,0.10], pos:[0,-0.465,0],  c:[0.90,0.90,0.90] },
    { size:[0.07,0.86,0.10], pos:[-0.565,0,0],  c:[0.90,0.90,0.90] },
    { size:[0.07,0.86,0.10], pos:[ 0.565,0,0],  c:[0.90,0.90,0.90] },
    { size:[0.05,0.86,0.08], pos:[0,0,0],       c:[0.90,0.90,0.90] },
    { size:[0.47,0.84,0.01], pos:[-0.28,0,0.01], c:[0.72,0.88,0.97], transparent:true, opacity:0.30, emissive:[0.35,0.55,0.75], emissiveIntensity:0.25 },
    { size:[0.47,0.84,0.01], pos:[ 0.28,0,0.01], c:[0.72,0.88,0.97], transparent:true, opacity:0.30, emissive:[0.35,0.55,0.75], emissiveIntensity:0.25 },
    { size:[1.06,0.86,0.02], pos:[0,0,-0.04],   c:[0.20,0.20,0.20] },
  ]},
  '액자': { w:0.8, d:0.06, h:0.6, wallItem:true, parts:[
    { size:[0.80,0.60,0.06], pos:[0,0,0],     c:[0.35,0.26,0.18] },
    { size:[0.68,0.48,0.01], pos:[0,0,0.032], c:[0.92,0.86,0.74] },
    { size:[0.04,0.48,0.04], pos:[-0.32,0,0.015], c:[0.30,0.22,0.14] },
    { size:[0.04,0.48,0.04], pos:[ 0.32,0,0.015], c:[0.30,0.22,0.14] },
    { size:[0.72,0.04,0.04], pos:[0, 0.28,0.015], c:[0.30,0.22,0.14] },
    { size:[0.72,0.04,0.04], pos:[0,-0.28,0.015], c:[0.30,0.22,0.14] },
  ]},
  '조명': { w:0.35, d:0.20, h:0.35, wallItem:true, parts:[
    { size:[0.12,0.18,0.10], pos:[0, 0.10,-0.04], c:[0.75,0.72,0.65] },
    { size:[0.30,0.22,0.22], pos:[0,-0.02, 0.06], c:[0.95,0.92,0.85] },
    { size:[0.08,0.08,0.06], pos:[0,-0.02, 0.05], c:[1.0, 0.98,0.85]  },
  ]},
  '에어컨': { w:0.80, d:0.22, h:0.28, wallItem:true, parts:[
    { size:[0.80,0.28,0.20], pos:[0, 0, 0.01], c:[0.95,0.95,0.95] },
    { size:[0.70,0.04,0.18], pos:[0,-0.10, 0.01], c:[0.85,0.85,0.85] },
    { size:[0.06,0.06,0.06], pos:[0.30, 0.08, 0.11], c:[0.4,0.8,1.0]  },
  ]},
  '스탠드 조명': { w:0.35, d:0.35, h:1.60, parts:[
    { size:[0.30,0.30,0.04], pos:[0,0,0],    c:[0.6,0.5,0.4] },
    { size:[0.04,0.04,1.30], pos:[0,0,0.67], c:[0.7,0.6,0.5] },
    { size:[0.35,0.35,0.20], pos:[0,0,1.50], c:[0.95,0.90,0.80] },
  ]},
  '커튼': { w:1.20, d:0.08, h:2.20, wallItem:true, parts:[
    { size:[1.25,0.06,0.06], pos:[0, 1.10, 0.04],  c:[0.5,0.4,0.35] },
    { size:[0.50,2.20,0.04], pos:[-0.34, 0, 0],    c:[0.80,0.65,0.55] },
    { size:[0.50,2.20,0.04], pos:[ 0.34, 0, 0],    c:[0.80,0.65,0.55] },
    { size:[0.10,2.20,0.04], pos:[ 0,    0, 0.02], c:[0.70,0.55,0.45] },
  ]},
  '시계': { w:0.30, d:0.06, h:0.35, wallItem:true, parts:[
    { size:[0.28,0.34,0.05], pos:[0, 0.02, 0], c:[0.85,0.90,0.85] },
    { size:[0.20,0.20,0.03], pos:[0,-0.04, 0.03], c:[0.95,0.92,0.85] },
    { size:[0.04,0.04,0.04], pos:[0,-0.14, 0.04], c:[0.90,0.85,0.75] },
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
 
function buildProceduralGroup(THREE, data) {
  const group = new THREE.Group()
  const yOffset = data.wallItem ? 0 : -data.halfH
  data.parts.forEach(p => {
    const geo = p.shape === 'pillow'
      ? new THREE.SphereGeometry(0.5, 24, 14)
      : new THREE.BoxGeometry(...p.size)
    let mat
    if (p.transparent) {
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
 
const WALL_ITEM_NAMES = new Set(['문', '창문', '액자', '그림', '거울', '에어컨', '시계', '커튼'])
const CEILING_ITEM_NAMES = new Set(['조명'])
const CEILING_OR_FLOOR_NAMES = new Set(['화분'])
 
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
  const { furnitureList, roomSize, roomColors, roomTextures, roomSurfaceTextures, roomBoxTextures, roomCameraPose, roomCameraPoseMode, roomMesh, reset, originalFile,
          savedDesignToLoad, clearSavedDesignToLoad, setSavedDesignToLoad, setStep } = useStore()
  const roomColorsMemo = useMemo(() => ({
    wall: roomColors?.wall || [0.9, 0.9, 0.9],
    floor: roomColors?.floor || [0.6, 0.4, 0.2]
  }), [roomColors])
 
  const [furnitureMeshes, setFurnitureMeshes] = useState({})
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
 
  const [viewMode, setViewMode] = useState('3d')
  const [generating, setGenerating] = useState(false)
  const [generatingId, setGeneratingId] = useState(null)

  useEffect(() => {
    if (!savedDesignToLoad) return
    const meshes = savedDesignToLoad.furnitureMeshes || {}
    setFurnitureMeshes(meshes)
    const rawPlaced = savedDesignToLoad.placedMeshes || {}
    const restored = {}
    Object.entries(rawPlaced).forEach(([instanceId, item]) => {
      const src = item.data ?? meshes[item.furnitureId] ?? meshes[parseInt(instanceId.split('_')[0])]
      if (!src) return
      const data = src.type === 'procedural' ? { ...src } : {
        ...src,
        vertices: src.vertices instanceof Float32Array ? src.vertices.slice() : new Float32Array(src.vertices),
        faces:    src.faces    instanceof Uint32Array  ? src.faces.slice()    : new Uint32Array(src.faces),
        colors:   src.colors   instanceof Float32Array ? src.colors.slice()   : new Float32Array(src.colors),
      }
      restored[instanceId] = { data, position: item.position, estimatedRealSize: item.estimatedRealSize }
    })
    setUndoState({ snapshots: [restored], idx: 0 })
    clearSavedDesignToLoad()
  }, [savedDesignToLoad])
 
  const handleSave = async () => {
    const canvas = document.querySelector('#room-viewer-wrap canvas')
    if (!canvas) { toast.error('뷰어를 찾을 수 없습니다'); return }
    const thumbnail = canvas.toDataURL('image/jpeg', 0.6)
    try {
      const placedRefs = {}
      Object.entries(placedMeshes).forEach(([instanceId, item]) => {
        const furnitureId = parseInt(instanceId.split('_')[0])
        placedRefs[instanceId] = { furnitureId, position: item.position, estimatedRealSize: item.estimatedRealSize }
      })
      await dbSaveDesign({ timestamp: Date.now(), thumbnail, furnitureMeshes, placedMeshes: placedRefs, furnitureList })
      toast.success('디자인 저장 완료! 업로드 화면에서 불러올 수 있습니다')
    } catch (e) {
      toast.error('저장 실패: ' + e.message)
    }
  }
 
  const handleDelete = (instanceId) => {
    setPlacedMeshes(prev => { const next = { ...prev }; delete next[instanceId]; return next })
  }

  const handleCopy = (instanceId) => {
    setPlacedMeshes(prev => {
      const original = prev[instanceId]
      if (!original) return prev
      const newId = `${Date.now()}`
      return { ...prev, [newId]: { ...original, position: { x: original.position.x + 0.5, z: original.position.z + 0.5 } } }
    })
  }
 
  const generate3DMesh = async (furniture) => {
    setGenerating(true)
    setGeneratingId(furniture.id)
    toast.success('SAM3D로 3D 메쉬 생성 중...')
    try {
      const imgBlob = await fetch(`data:image/png;base64,${furniture.b64}`).then(r => r.blob())
      const imgFile = new File([imgBlob], 'furniture.png', { type: 'image/png' })
      const form = new FormData()
      form.append('image', imgFile)
      form.append('category', furniture.name || '')
 
      const THIN_FLAT = ['조명', '꽃', '화분', '시계', '액자', '그림', '거울']
      if (THIN_FLAT.some(k => (furniture.name || '').includes(k)) && originalFile && furniture.bbox) {
        form.append('full_image', originalFile)
        form.append('bbox', JSON.stringify(furniture.bbox))
      }
 
      const res = await fetch('http://127.0.0.1:8001/api/sam3d/mesh', { method: 'POST', body: form })
      const data = await res.json()
      if (!data.success) throw new Error(data.error || '3D 생성 실패')
      const raw = data.mesh
      const processed = data.type === 'textured'
        ? { type: 'textured', vertices: new Float32Array(raw.vertices.flat()), faces: new Uint32Array(raw.faces.flat()), uvs: new Float32Array(raw.uvs.flat()), textureB64: raw.textureB64 }
        : { type: 'vertex_color', vertices: new Float32Array(raw.vertices.flat()), faces: new Uint32Array(raw.faces.flat()), colors: new Float32Array(raw.colors.flat()) }
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
    if (String(furnitureId).startsWith('preset_')) {
      const name = String(furnitureId).replace('preset_', '')
      const def = findProceduralDef(name)
      if (!def) return
      const data = { type: 'procedural', name, wallItem: def.wallItem || false, halfH: def.h / 2, halfFX: def.w / 2, halfFZ: def.d / 2, parts: def.parts }
      const instanceId = `${furnitureId}_${Date.now()}`
      setPlacedMeshes(prev => ({ ...prev, [instanceId]: { data, position, estimatedRealSize: def.w } }))
      return
    }

    const numId = Number(furnitureId)
    const meshData = furnitureMeshes[numId]
    if (!meshData) { toast.error('먼저 3D 변환을 해주세요!'); return }

    // 이름 매칭(findStdSize)이 안 되는 가구의 크기 추정값(estimatedRealSize, 렌더링 시
    // "실제 높이" 기준으로 사용됨). 카메라 pose + 원본 사진 속 픽셀 bbox로 계산 —
    // 원근을 반영하므로 이전의 "제일 큰 가구 대비 픽셀 비율" 방식보다 정확.
    // 필요 데이터(카메라 pose, bbox)가 없으면 방 크기 비례 값으로 폴백.
    const furniture = furnitureList.find(f => f.id === numId)
    const computedHeight = estimateFurnitureHeightM(roomCameraPose, furniture?.bbox)
    const estimatedRealSize = computedHeight ?? roomSize.width * 0.2
    const camHeight = estimateCameraHeightM(roomCameraPose)
    console.log(`[가구 실제 크기 추정] ${furniture?.name ?? numId}: ${estimatedRealSize.toFixed(3)}m`,
      computedHeight != null
        ? `(카메라 pose 기반 계산값, mode=${roomCameraPoseMode ?? 'unknown'}, 카메라 추정 높이=${camHeight != null ? (camHeight * 100).toFixed(1) + 'cm' : 'N/A'})`
        : '(폴백: 방 가로 × 0.2)')

    const instanceId = `${numId}_${Date.now()}`
    setPlacedMeshes(prev => ({ ...prev, [instanceId]: { data: meshData, position, estimatedRealSize } }))
  }
 
  return (
    <div style={{
      position: 'relative', width: '100vw', marginLeft: 'calc(50% - 50vw)',
      height: 'calc(100vh - 110px)', overflow: 'hidden', background: '#000000'
    }}>
      <div id="room-viewer-wrap" style={{ position: 'absolute', inset: 0 }}>
        <RoomViewer
          roomSize={roomSize}
          roomColors={roomColorsMemo}
          roomTextures={roomTextures}
          roomSurfaceTextures={roomSurfaceTextures}
          roomBoxTextures={roomBoxTextures}
          roomMesh={roomMesh}
          placedMeshes={placedMeshes}
          onDrop={handleDrop}
          onDelete={handleDelete}
          onCopy={handleCopy}
          viewMode={viewMode}
        />
      </div>
 
      <div style={{
        position: 'absolute', top: 0, left: 0, right: 0, zIndex: 10,
        background: 'linear-gradient(to bottom, rgba(0,0,0,0.75), transparent)',
        padding: '16px 24px', display: 'flex', alignItems: 'center', gap: '16px'
      }}>
        <h2 style={{ margin: 0, fontSize: '18px', fontWeight: 700 }}>3D 인테리어 배치</h2>
        <span style={{ color: '#aaa', fontSize: '13px' }}>방 크기: {roomSize.width}m × {roomSize.depth}m × {roomSize.height}m</span>
        <span style={{ color: '#aaa', fontSize: '13px' }}>변환 {Object.keys(furnitureMeshes).length}개 · 배치 {Object.keys(placedMeshes).length}개</span>
        <div style={{ marginLeft: 'auto', display: 'flex', background: 'rgba(0,0,0,0.5)', borderRadius: '20px', padding: '3px' }}>
          {['2D','3D'].map(m => (
            <button key={m} onClick={() => setViewMode(m.toLowerCase())} style={{
              padding: '5px 16px', border: 'none', borderRadius: '16px', cursor: 'pointer',
              fontWeight: 700, fontSize: '13px', transition: 'all 0.2s',
              background: viewMode === m.toLowerCase() ? '#27ae60' : 'transparent',
              color: viewMode === m.toLowerCase() ? '#fff' : '#aaa',
            }}>{m}</button>
          ))}
        </div>
      </div>
 
      <div style={{
        position: 'absolute', top: '60px', left: '16px', bottom: '16px', width: '200px', zIndex: 10,
        background: 'rgba(20,20,20,0.85)', backdropFilter: 'blur(12px)',
        borderRadius: '16px', border: '1px solid rgba(255,255,255,0.08)',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        <div style={{ padding: '14px 14px 8px', borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
          <h3 style={{ margin: 0, fontSize: '13px', color: '#ddd', fontWeight: 600 }}>추출된 가구</h3>
        </div>
        {/* 문 추가 섹션 */}
        <div style={{ padding: '8px 10px', borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
          <div style={{ fontSize: '10px', color: '#666', letterSpacing: '0.06em', marginBottom: '6px' }}>벽 설치 아이템</div>
          {[
            { id: 'preset_문', name: '문', icon: '🚪', desc: '0.9×2.1m' },
            { id: 'preset_창문', name: '창문', icon: '🪟', desc: '1.2×1.0m' },
          ].map(item => (
            <div key={item.id}
              draggable
              onDragStart={(e) => {
                e.dataTransfer.setData('furnitureId', item.id)
                e.dataTransfer.setData('furnitureName', item.name)
              }}
              style={{
                display: 'flex', alignItems: 'center', gap: '8px',
                background: 'rgba(255,255,255,0.06)', borderRadius: '8px',
                padding: '7px 10px', marginBottom: '5px',
                border: '1px solid rgba(255,255,255,0.1)',
                cursor: 'grab',
              }}
            >
              <span style={{ fontSize: '18px' }}>{item.icon}</span>
              <div>
                <div style={{ fontSize: '12px', color: '#ddd', fontWeight: 600 }}>{item.name}</div>
                <div style={{ fontSize: '10px', color: '#666' }}>{item.desc}</div>
              </div>
            </div>
          ))}
        </div>
        <div style={{ flex: 1, overflowY: 'auto', padding: '10px' }}>
          {furnitureList.map((f, i) => (
            <div key={i}
              draggable={!!furnitureMeshes[f.id]}
              onDragStart={(e) => {
                if (furnitureMeshes[f.id]) {
                  e.dataTransfer.setData('furnitureId', String(f.id))
                  e.dataTransfer.setData('furnitureName', f.name || '')
                }
              }}
              style={{
                background: 'rgba(255,255,255,0.05)', borderRadius: '10px', padding: '8px', marginBottom: '10px',
                border: furnitureMeshes[f.id] ? '1.5px solid #27ae60' : '1.5px solid rgba(255,255,255,0.07)',
                cursor: furnitureMeshes[f.id] ? 'grab' : 'default', transition: 'border-color 0.2s',
              }}
            >
              {furnitureMeshes[f.id]
                ? <MiniMeshViewer data={furnitureMeshes[f.id]} />
                : <img src={`data:image/png;base64,${f.b64}`} style={{ width: '100%', borderRadius: '6px' }} />
              }
              <p style={{ fontSize: '11px', color: '#bbb', margin: '6px 0 4px', textAlign: 'center' }}>{f.name || `가구 ${i + 1}`}</p>
              {furnitureMeshes[f.id] && (
                <p style={{ fontSize: '10px', color: '#27ae60', textAlign: 'center', marginBottom: '4px' }}>✅ 드래그해서 배치</p>
              )}
              <button onClick={() => generate3DMesh(f)} disabled={generating} style={{
                width: '100%', padding: '6px', marginTop: '2px',
                background: generating && generatingId === f.id ? '#444' : furnitureMeshes[f.id] ? '#27ae60' : '#3498db',
                color: 'white', border: 'none', borderRadius: '6px',
                cursor: generating ? 'not-allowed' : 'pointer', fontSize: '11px', fontWeight: 600,
              }}>
                {generating && generatingId === f.id ? '생성 중...' : furnitureMeshes[f.id] ? '🔄 재생성' : '🔷 3D 변환'}
              </button>
            </div>
          ))}
        </div>
      </div>
 
      <div style={{ position: 'absolute', bottom: '24px', right: '24px', zIndex: 10, display: 'flex', flexDirection: 'column', gap: '8px' }}>
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
          color: 'white', border: 'none', borderRadius: '10px', cursor: 'pointer', fontSize: '12px', fontWeight: 600,
        }}>💾 디자인 저장</button>
        <button onClick={() => setPlacedMeshes({})} style={{
          padding: '10px 16px', background: 'rgba(30,30,30,0.85)', backdropFilter: 'blur(8px)',
          color: '#ddd', border: '1px solid rgba(255,255,255,0.1)',
          borderRadius: '10px', cursor: 'pointer', fontSize: '12px', fontWeight: 600,
        }}>🗑️ 배치 초기화</button>
        <button onClick={() => setStep('roommaking')} style={{
          padding: '10px 16px', background: 'rgba(30,30,30,0.85)', backdropFilter: 'blur(8px)',
          color: '#ddd', border: '1px solid rgba(255,255,255,0.1)',
          borderRadius: '10px', cursor: 'pointer', fontSize: '12px', fontWeight: 600,
        }}>← 방 만들기로</button>
        <button onClick={reset} style={{
          padding: '10px 16px', background: 'rgba(231,76,60,0.85)', backdropFilter: 'blur(8px)',
          color: 'white', border: 'none', borderRadius: '10px', cursor: 'pointer', fontSize: '12px', fontWeight: 600,
        }}>홈으로</button>
      </div>
    </div>
  )
}
 