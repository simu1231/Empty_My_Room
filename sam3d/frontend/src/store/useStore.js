import { create } from 'zustand'
export const useStore = create((set) => ({
  step: 'upload',
  setStep: (step) => set({ step }),
  originalFile: null,
  originalUrl: null,
  setOriginalImage: (file) => {
    const url = URL.createObjectURL(file)
    set({ originalFile: file, originalUrl: url })
  },
  clickPoints: [],
  addPoint: (pt) => set((s) => ({ clickPoints: [...s.clickPoints, pt] })),
  removeLastPoint: () => set((s) => ({ clickPoints: s.clickPoints.slice(0, -1) })),
  clearPoints: () => set({ clickPoints: [] }),
  maskB64: null,
  setMask: (b64) => set({ maskB64: b64 }),
  // 빈방 이미지
  emptyRoomUrl: null,
  emptyRoomFile: null,
  setEmptyRoom: (url, file) => set({ emptyRoomUrl: url, emptyRoomFile: file }),
  // 추출된 가구 리스트
  furnitureList: [],
  setFurnitureList: (list) => set({ furnitureList: list }),
  // 방 크기 설정
  roomSize: { width: 5, depth: 4, height: 2.5 },
  setRoomSize: (size) => set({ roomSize: size }),
  // 방 색상
 // 방 색상 부분을 이렇게 교체
  roomColors: { wall: [0.9, 0.9, 0.9], floor: [0.6, 0.4, 0.2] },
  roomTextures: { wall: null, floor: null },
  setRoomColors: (colors) => set({ roomColors: colors }),
  setRoomTextures: (textures) => set({ roomTextures: textures }),
  roomSurfaceTextures: null,
  setRoomSurfaceTextures: (t) => set({ roomSurfaceTextures: t }),
  // uLayout 기반 rectify된 벽/바닥/천장 실사 텍스처 ({ back_wall, left_wall, right_wall, floor, ceiling } base64 JPEG)
  roomBoxTextures: null,
  setRoomBoxTextures: (t) => set({ roomBoxTextures: t }),
  // uLayout solvePnP 카메라 pose ({rotation_matrix, translation, K, image_size}) — 가구 실제
  // 크기 추정(광선-바닥평면 교차)에 재사용. 인페인팅이 화각/해상도를 안 바꾸므로 원본
  // (가구 있는) 사진에도 그대로 적용 가능.
  roomCameraPose: null,
  setRoomCameraPose: (p) => set({ roomCameraPose: p }),
  roomMesh: null,
  setRoomMesh: (mesh) => set({ roomMesh: mesh }),
  loading: false,
  loadingMsg: '',
  setLoading: (loading, msg = '') => set({ loading, loadingMsg: msg }),
  // 저장된 디자인 불러오기용
  savedDesignToLoad: null,
  setSavedDesignToLoad: (design) => set({ savedDesignToLoad: design }),
  clearSavedDesignToLoad: () => set({ savedDesignToLoad: null }),
  reset: () => set({
    step: 'upload',
    originalFile: null,
    originalUrl: null,
    clickPoints: [],
    maskB64: null,
    emptyRoomUrl: null,
    emptyRoomFile: null,
    furnitureList: [],
    roomSize: { width: 5, depth: 4, height: 2.5 },
    roomColors: { wall: [0.9, 0.9, 0.9], floor: [0.6, 0.4, 0.2] },
    roomTextures: { wall: null, floor: null },
    roomSurfaceTextures: null,
    roomBoxTextures: null,
    roomCameraPose: null,
    roomMesh: null,
    loading: false,
    loadingMsg: '',
  }),
}))