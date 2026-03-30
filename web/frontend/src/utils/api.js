import axios from 'axios'

const api = axios.create({
  baseURL: 'http://localhost:8000',
  timeout: 120000,
})

// base64 → blob URL 변환
export function b64ToUrl(b64, mime = 'image/jpeg') {
  const bin = atob(b64)
  const arr = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i)
  const blob = new Blob([arr], { type: mime })
  return URL.createObjectURL(blob)
}

// base64 → File 변환
export function b64ToFile(b64, filename, mime = 'image/png') {
  const bin = atob(b64)
  const arr = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i)
  const blob = new Blob([arr], { type: mime })
  return new File([blob], filename, { type: mime })
}

// SAM2 마스크 생성
export async function segmentMask(imageFile, points) {
  const form = new FormData()
  form.append('image', imageFile)
  form.append('points', JSON.stringify(points))
  const { data } = await api.post('/api/segment/mask', form)
  return data
}

// LaMa 가구 제거
export async function inpaintRemove(imageFile, maskFile) {
  const form = new FormData()
  form.append('image', imageFile)
  form.append('mask', maskFile)
  const { data } = await api.post('/api/inpaint/remove', form)
  return data
}