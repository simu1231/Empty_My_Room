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
  loading: false,
  loadingMsg: '',
  setLoading: (loading, msg = '') => set({ loading, loadingMsg: msg }),
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
    loading: false,
    loadingMsg: '',
  }),
}))