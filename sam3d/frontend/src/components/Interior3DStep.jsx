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

function RoomViewer({ roomSize, roomColors, roomTextures, placedMeshes, onDrop }) {
  const createDynamicTexture = (baseColor, type = 'plank') => {
  const canvas = document.createElement('canvas');
  canvas.width = 512;
  canvas.height = 512;
  const ctx = canvas.getContext('2d');

  // 1. 사진에서 추출된 배경색 채우기
  ctx.fillStyle = baseColor; 
  ctx.fillRect(0, 0, 512, 512);

  // 2. 그래픽 선 설정 (매우 연한 검정색으로 깔끔하게)
  ctx.strokeStyle = 'rgba(0, 0, 0, 0.15)';
  ctx.lineWidth = 2;

  if (type === 'grid') {
    // 격자가 꼭 필요할 때만 쓰는 루프
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.1)';
    for (let i = 0; i <= 512; i += 64) {
      ctx.beginPath(); ctx.moveTo(i, 0); ctx.lineTo(i, 512); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, i); ctx.lineTo(512, i); ctx.stroke();
    }
  } else {
    // [개선된 나무 무늬] 가로 줄을 최소화해서 격자 느낌 제거
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.07)'; // 선을 더 연하고 부드럽게
    ctx.lineWidth = 1;

    const plankWidth = 48; // 판자 너비 (숫자가 작을수록 촘촘함)
    
    for (let x = 0; x <= 512; x += plankWidth) {
      // 1. 세로 줄 (판자 경계선) - 이건 길게 쭉 긋습니다.
      ctx.beginPath(); 
      ctx.moveTo(x, 0); 
      ctx.lineTo(x, 512); 
      ctx.stroke();

      // 2. 가로 마디 (Joint) - 아주 가끔만 그려서 격자 방지
      // x 위치에 따라 시작점을 다르게 해서 엇갈리게 만듭니다.
      let startY = (x % (plankWidth * 2) === 0) ? 0 : 200; 
      for (let y = startY; y <= 512; y += 400) { // 400px 간격으로 아주 길게 배치
        ctx.beginPath(); 
        ctx.moveTo(x, y); 
        ctx.lineTo(x + plankWidth, y); 
        ctx.stroke();
      }
    }
  }

  const texture = new window.THREE.CanvasTexture(canvas);
  texture.wrapS = texture.wrapT = window.THREE.RepeatWrapping;
  texture.repeat.set(2, 2); // 방 크기에 맞춰 적절히 반복
  return texture;
};
  const mountRef = useRef(null)
  const sceneRef = useRef(null)
  const placedGroupsRef = useRef({})
  const selectedObjRef = useRef(null)
  const floorRef = useRef(null)

  useEffect(() => {
    if (!mountRef.current) return;
    const THREE = window.THREE;
    const el = mountRef.current;
    const width = el.clientWidth;
    const height = 600;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x2a2a2a);
    sceneRef.current = scene;

    const camera = new THREE.PerspectiveCamera(45, width / height, 0.01, 1000);
    camera.position.set(roomSize.width, roomSize.height * 2, roomSize.depth * 2);
    camera.lookAt(0, 0, 0);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(width, height);
    el.appendChild(renderer.domElement);

    const raycaster = new THREE.Raycaster();

    // 조명 (현실감 있게 밝기 조절)
    scene.add(new THREE.AmbientLight(0xffffff, 0.9));
    const dir1 = new THREE.DirectionalLight(0xffffff, 0.5);
    dir1.position.set(5, 10, 5);
    scene.add(dir1);

    const w = roomSize.width;
    const h = roomSize.height;
    const d = roomSize.depth;
    const wc = roomColors.wall;
    const fc = roomColors.floor;

    const fColor = `rgb(${fc[0]*255}, ${fc[1]*255}, ${fc[2]*255})`;
    const wColor = `rgb(${wc[0]*255}, ${wc[1]*255}, ${wc[2]*255})`;

    // [1] 바닥 생성 - 나무 판자 느낌 강제 적용
    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(w, d),
      new THREE.MeshStandardMaterial({ 
        map: createDynamicTexture(fColor, 'plank'), // 'plank' 타입 고정
        roughness: 0.8 
      })
    );
    floor.rotation.x = -Math.PI / 2;
    floor.position.y = -h / 2;
    floor.name = 'floor';
    scene.add(floor);
    floorRef.current = floor;

    // [2] 벽면 생성 - 무늬 없는 깨끗한 그래픽 (색상만 사용)
    const wallMaterial = new THREE.MeshStandardMaterial({ 
      color: new THREE.Color(wc[0], wc[1], wc[2]), 
      roughness: 1.0 
    });

    // 뒷벽
    const backWall = new THREE.Mesh(new THREE.PlaneGeometry(w, h), wallMaterial);
    backWall.position.z = -d / 2;
    scene.add(backWall);

    // 왼쪽 벽
    const leftWall = new THREE.Mesh(new THREE.PlaneGeometry(d, h), wallMaterial);
    leftWall.rotation.y = Math.PI / 2;
    leftWall.position.x = -w / 2;
    scene.add(leftWall);

    // 오른쪽 벽
    const rightWall = new THREE.Mesh(new THREE.PlaneGeometry(d, h), wallMaterial);
    rightWall.rotation.y = -Math.PI / 2;
    rightWall.position.x = w / 2;
    scene.add(rightWall);

    // 천장
    const ceiling = new THREE.Mesh(
      new THREE.PlaneGeometry(w, d),
      new THREE.MeshStandardMaterial({ color: 0xffffff })
    );
    ceiling.rotation.x = Math.PI / 2;
    ceiling.position.y = h / 2;
    scene.add(ceiling);

    // --- 이벤트 리스너 및 애니메이션 (이전과 동일) ---
    const getMousePos = (e) => {
      const rect = el.getBoundingClientRect();
      return {
        x: ((e.clientX - rect.left) / width) * 2 - 1,
        y: -((e.clientY - rect.top) / height) * 2 + 1,
      };
    };

    let isRotating = false;
    let prevX = 0, prevY = 0;
    let isDraggingFurniture = false;

    const onMouseDown = (e) => {
      const mouse = getMousePos(e);
      raycaster.setFromCamera(mouse, camera);
      const placedObjects = Object.values(placedGroupsRef.current);
      const intersects = raycaster.intersectObjects(placedObjects, true);

      if (intersects.length > 0 && e.button === 0) {
        let obj = intersects[0].object;
        while (obj.parent && !placedObjects.includes(obj)) obj = obj.parent;
        selectedObjRef.current = obj;
        isDraggingFurniture = true;
      } else {
        isRotating = true;
        prevX = e.clientX;
        prevY = e.clientY;
      }
    };

    const onMouseMove = (e) => {
      if (isDraggingFurniture && selectedObjRef.current) {
        const mouse = getMousePos(e);
        raycaster.setFromCamera(mouse, camera);
        const intersects = raycaster.intersectObject(floorRef.current);
        if (intersects.length > 0) {
          selectedObjRef.current.position.x = intersects[0].point.x;
          selectedObjRef.current.position.z = intersects[0].point.z;
        }
      } else if (isRotating) {
        const dx = e.clientX - prevX;
        const dy = e.clientY - prevY;
        const spherical = new THREE.Spherical().setFromVector3(camera.position);
        spherical.theta -= dx * 0.01;
        spherical.phi -= dy * 0.005;
        spherical.phi = Math.max(0.1, Math.min(Math.PI / 2, spherical.phi));
        camera.position.setFromSpherical(spherical);
        camera.lookAt(0, 0, 0);
        prevX = e.clientX;
        prevY = e.clientY;
      }
    };

    const onMouseUp = () => {
      selectedObjRef.current = null;
      isDraggingFurniture = false;
      isRotating = false;
    };

    renderer.domElement.addEventListener('mousedown', onMouseDown);
    renderer.domElement.addEventListener('mousemove', onMouseMove);
    renderer.domElement.addEventListener('mouseup', onMouseUp);

    let animId;
    const animate = () => { animId = requestAnimationFrame(animate); renderer.render(scene, camera); };
    animate();

    return () => {
      cancelAnimationFrame(animId);
      renderer.dispose();
      if (el.contains(renderer.domElement)) el.removeChild(renderer.domElement);
    };
  }, [roomSize, roomColors, roomTextures]);

  useEffect(() => {
    if (!sceneRef.current) return
    const THREE = window.THREE

    Object.entries(placedMeshes).forEach(([instanceId, { data, position }]) => {
      if (placedGroupsRef.current[instanceId]) return

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
      const scale = 1.0 / maxDim

      const material = new THREE.MeshStandardMaterial({ vertexColors: true, side: THREE.DoubleSide })
      const mesh = new THREE.Mesh(geometry, material)
      mesh.rotation.x = -Math.PI / 2
      mesh.scale.set(scale, scale, scale)

      const group = new THREE.Group()
      group.add(mesh)
      group.position.set(position.x, -roomSize.height / 2 + 0.5, position.z)
      sceneRef.current.add(group)
      placedGroupsRef.current[instanceId] = group
    })
  }, [placedMeshes])

  return (
    <div>
      <p style={{ color: '#aaa', fontSize: '12px', marginBottom: '8px' }}>
        🖱️ 왼쪽 가구를 드래그해서 방에 놓으세요 · 배치된 가구 좌클릭으로 이동 · 드래그로 카메라 회전
      </p>
      <div ref={mountRef} style={{ width: '100%', height: '600px', borderRadius: '8px', overflow: 'hidden', cursor: 'grab' }} />
    </div>
  )
}

export default function Interior3DStep() {
 const { furnitureList, roomSize, roomColors, roomTextures, reset } = useStore()
  const wallColor = roomColors?.wall || [0.9, 0.9, 0.9]
  const floorColor = roomColors?.floor || [0.6, 0.4, 0.2]
  const roomColorsMemo = useMemo(() => ({
    wall: roomColors?.wall || [0.9, 0.9, 0.9],
    floor: roomColors?.floor || [0.6, 0.4, 0.2]
  }), [roomColors]);

  const [furnitureMeshes, setFurnitureMeshes] = useState({})
  const [placedMeshes, setPlacedMeshes] = useState({})
  const [generating, setGenerating] = useState(false)
  const [generatingId, setGeneratingId] = useState(null)

  const generate3DMesh = async (furniture) => {
    setGenerating(true)
    setGeneratingId(furniture.id)
    toast.success('SAM3D로 3D 메쉬 생성 중... (20~30분 소요)')
    try {
      const imgBlob = await fetch(`data:image/png;base64,${furniture.b64}`).then(r => r.blob())
      const imgFile = new File([imgBlob], 'furniture.png', { type: 'image/png' })
      const form = new FormData()
      form.append('image', imgFile)
      const res = await fetch('http://127.0.0.1:48888/api/sam3d/mesh', { method: 'POST', body: form })
      const data = await res.json()
      if (!data.success) throw new Error(data.error || '3D 생성 실패')
      setFurnitureMeshes(prev => ({ ...prev, [furniture.id]: data.mesh }))
      toast.success(`3D 변환 완료! 드래그해서 방에 배치하세요 🎉`)
    } catch (e) {
      toast.error(`3D 생성 실패: ${e.message}`)
    } finally {
      setGenerating(false)
      setGeneratingId(null)
    }
  }

  const handleDrop = (furnitureId, position) => {
    const meshData = furnitureMeshes[furnitureId]
    if (!meshData) {
      toast.error('먼저 3D 변환을 해주세요!')
      return
    }
    const instanceId = `${furnitureId}_${Date.now()}`
    setPlacedMeshes(prev => ({ ...prev, [instanceId]: { data: meshData, position } }))
    toast.success('가구 배치 완료! 🎉')
  }

  return (
    <div style={{ padding: '20px' }}>
      <h2>3D 인테리어 배치</h2>
      <p style={{ color: '#aaa' }}>방 크기: {roomSize.width}m × {roomSize.depth}m × {roomSize.height}m</p>

      <div style={{ display: 'flex', gap: '20px', marginTop: '16px' }}>
        <div style={{ width: '200px', background: '#1a1a1a', borderRadius: '12px', padding: '12px', overflowY: 'auto', maxHeight: '700px' }}>
          <h3 style={{ marginBottom: '12px', fontSize: '14px' }}>추출된 가구</h3>
          {furnitureList.map((f, i) => (
            <div
              key={i}
              draggable={!!furnitureMeshes[f.id]}
              onDragStart={(e) => {
                if (furnitureMeshes[f.id]) {
                  e.dataTransfer.setData('furnitureId', String(f.id))
                }
              }}
              style={{
                background: '#2a2a2a',
                borderRadius: '8px', padding: '8px', marginBottom: '12px',
                border: furnitureMeshes[f.id] ? '2px solid #27ae60' : '2px solid transparent',
                cursor: furnitureMeshes[f.id] ? 'grab' : 'default',
              }}
            >
              {furnitureMeshes[f.id] ? (
                <MiniMeshViewer data={furnitureMeshes[f.id]} />
              ) : (
                <img src={`data:image/png;base64,${f.b64}`} style={{ width: '100%', borderRadius: '4px' }} />
              )}
              <p style={{ fontSize: '12px', color: '#aaa', marginTop: '6px', textAlign: 'center' }}>
                {f.name || `가구 ${i + 1}`}
              </p>
              {furnitureMeshes[f.id] && (
                <p style={{ fontSize: '10px', color: '#27ae60', textAlign: 'center', marginBottom: '4px' }}>
                  ✅ 드래그해서 방에 배치
                </p>
              )}
              <button
                onClick={() => generate3DMesh(f)}
                disabled={generating}
                style={{
                  width: '100%', padding: '6px', marginTop: '4px',
                  background: generating && generatingId === f.id ? '#555' : furnitureMeshes[f.id] ? '#27ae60' : '#3498db',
                  color: 'white', border: 'none', borderRadius: '6px',
                  cursor: generating ? 'not-allowed' : 'pointer', fontSize: '11px'
                }}
              >
                {generating && generatingId === f.id ? '생성 중...' : furnitureMeshes[f.id] ? '🔄 재생성' : '🔷 3D 변환'}
              </button>
            </div>
          ))}
        </div>

        <div style={{ flex: 1 }}>
          <RoomViewer
             roomSize={roomSize}
             roomColors={roomColorsMemo}
             roomTextures={roomTextures}
             placedMeshes={placedMeshes}
             onDrop={handleDrop}
          />
        </div>

        <div style={{ width: '150px', background: '#1a1a1a', borderRadius: '12px', padding: '12px' }}>
          <h3 style={{ marginBottom: '12px', fontSize: '14px' }}>컨트롤</h3>
          <p style={{ fontSize: '12px', color: '#aaa', marginBottom: '4px' }}>변환: {Object.keys(furnitureMeshes).length}개</p>
          <p style={{ fontSize: '12px', color: '#aaa', marginBottom: '12px' }}>배치: {Object.keys(placedMeshes).length}개</p>
          <button
            onClick={() => setPlacedMeshes({})}
            style={{ width: '100%', padding: '10px', marginBottom: '8px', background: '#333', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '12px' }}
          >
            🗑️ 배치 초기화
          </button>
          <button onClick={reset} style={{ width: '100%', padding: '10px', background: '#e74c3c', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '12px' }}>
            🔄 처음부터
          </button>
        </div>
      </div>
    </div>
  )
}
