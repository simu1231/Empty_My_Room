import { create } from 'zustand'

export const useStore = create((set) => ({
  // 현재 단계
  step: 'upload',
  setStep: (step) => set({ step }),

  // 이미지
  originalFile: null,
  originalUrl: null,
  setOriginalImage: (file) => {
    const url = URL.createObjectURL(file)
    set({ originalFile: file, originalUrl: url })
  },

  // 클릭 포인트 (가구 위치)
  clickPoints: [],
  addPoint: (pt) => set((s) => ({ clickPoints: [...s.clickPoints, pt] })),
  clearPoints: () => set({ clickPoints: [] }),

  // 마스크 (SAM2 결과)
  maskB64: null,
  setMask: (b64) => set({ maskB64: b64 }),

  // 인페인팅 결과 (LaMa 결과)
  inpaintedUrl: null,
  setInpainted: (url) => set({ inpaintedUrl: url }),

  // 로딩
  loading: false,
  loadingMsg: '',
  setLoading: (loading, msg = '') => set({ loading, loadingMsg: msg }),

  // 전체 리셋
  reset: () => set({
    step: 'upload',
    originalFile: null,
    originalUrl: null,
    clickPoints: [],
    maskB64: null,
    inpaintedUrl: null,
    loading: false,
    loadingMsg: '',
  }),
}))