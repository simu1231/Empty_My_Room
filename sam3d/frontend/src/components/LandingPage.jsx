import { useCallback, useEffect, useRef, useState } from 'react'
import './LandingPage.css'

function CompareSlider({ before, after, beforeLabel = 'Before', afterLabel = 'After' }) {
  const containerRef = useRef(null)
  const draggingRef = useRef(false)
  const [pos, setPos] = useState(50)

  const updateFromClientX = useCallback((clientX) => {
    const el = containerRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const ratio = ((clientX - rect.left) / rect.width) * 100
    setPos(Math.min(100, Math.max(0, ratio)))
  }, [])

  const handlePointerDown = (e) => {
    draggingRef.current = true
    e.currentTarget.setPointerCapture?.(e.pointerId)
    updateFromClientX(e.clientX)
  }
  const handlePointerMove = (e) => {
    if (!draggingRef.current) return
    updateFromClientX(e.clientX)
  }
  const stopDragging = () => { draggingRef.current = false }

  return (
    <div
      className="lp-compare"
      ref={containerRef}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={stopDragging}
      onPointerCancel={stopDragging}
    >
      <img className="lp-compare-base" src={before} alt={beforeLabel} draggable={false} />
      <div className="lp-compare-reveal" style={{ clipPath: `inset(0 0 0 ${pos}%)` }}>
        <img src={after} alt={afterLabel} draggable={false} />
      </div>
      <div className="lp-compare-handle" style={{ left: `${pos}%` }}>
        <span className="lp-compare-grip">&#8596;</span>
      </div>
      <span className="lp-compare-tag lp-compare-tag-before">{beforeLabel}</span>
      <span className="lp-compare-tag lp-compare-tag-after">{afterLabel}</span>
    </div>
  )
}

function DemoStage({ src, title, lead, features }) {
  const frameRef = useRef(null)
  const videoRef = useRef(null)
  const hasPlayedRef = useRef(false)

  useEffect(() => {
    const frame = frameRef.current
    const video = videoRef.current
    if (!frame || !video) return

    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting && !hasPlayedRef.current) {
          hasPlayedRef.current = true
          video.currentTime = 0
          video.play().catch(() => {})
        }
      })
    }, { threshold: 0.5 })

    observer.observe(frame)
    return () => observer.disconnect()
  }, [])

  return (
    <div className="lp-demo-stage">
      <div className="lp-room-frame" ref={frameRef}>
        <video ref={videoRef} muted playsInline loop preload="auto">
          <source src={src} type="video/mp4" />
        </video>
      </div>
      <div className="lp-demo-copy">
        <h3>{title}</h3>
        <p className="lp-demo-copy-lead">{lead}</p>
        <ul className="lp-feature-list">
          {features.map((f) => <li key={f}>{f}</li>)}
        </ul>
      </div>
    </div>
  )
}

export default function LandingPage({ onStart }) {
  return (
    <div className="landing-page">
      <nav className="lp-nav">
        <div className="lp-wrap lp-nav-inner">
          <span className="lp-logo">MyEmptyRoom</span>
          <button className="lp-btn lp-btn-primary lp-nav-btn" onClick={onStart}>시작하기</button>
        </div>
      </nav>

      <div className="lp-hero">
        <div className="lp-wrap lp-hero-grid">
          <div className="lp-hero-text">
            <h1>사진 한 장으로<br />내 방을 채워보세요</h1>
            <p>방 사진 하나만 올리면<br />가구가 배치된 모습을 3D로 미리 볼 수 있어요.<br />스크롤해서 어떻게 채워지는지 확인해보세요.</p>
            <div className="lp-hero-actions">
              <button className="lp-btn lp-btn-primary lp-btn-compact" onClick={onStart}>사진 업로드</button>
              <a href="#lp-how" className="lp-btn lp-btn-secondary lp-btn-compact">작동 방식 보기</a>
            </div>
          </div>
          <div className="lp-hero-visual">
            <CompareSlider
              before="/hero-before.jpg"
              after="/hero-after.jpg"
              beforeLabel="Before"
              afterLabel="After"
            />
          </div>
        </div>
      </div>

      <section id="lp-how" className="lp-surface-soft">
        <div className="lp-wrap lp-wrap-wide">
          <h2>가구를 클릭 한 번으로 인식해요</h2>
          <p className="lp-section-sub">화면 속 가구를 클릭하면 AI가 정확한 윤곽을 찾아 선택해줘요.</p>
          <DemoStage
            src="/room-demo-segment.mp4"
            title="정확한 가구 인식"
            lead={<>가구를 하나씩 클릭하기만 하면, <br className="lp-lead-break" />AI가 그 경계를 픽셀 단위로 찾아내요.</>}
            features={[
              '클릭 몇 번으로 원하는 가구 선택',
              'AI가 가구의 정확한 윤곽을 자동 추출',
              '여러 개 가구도 한 번에 선택 가능',
              '선택한 가구는 빈 방에서 자동으로 제거',
            ]}
          />
        </div>
      </section>

      <section className="lp-surface-soft">
        <div className="lp-wrap lp-wrap-wide">
          <h2>빈 방을 그대로 복원해요</h2>
          <p className="lp-section-sub">가구를 걷어낸 빈 방을 만들고, 벽과 바닥의 질감까지 분석해요.</p>
          <DemoStage
            src="/room-demo-roommaking.mp4"
            title="빈 방 생성 &amp; 색상 분석"
            lead={<>선택한 가구를 지운 빈 방 이미지를 만들고, <br className="lp-lead-break" />벽과 바닥의 색상·질감을 자동으로 분석해요.</>}
            features={[
              '가구를 제거한 빈 방 이미지 자동 생성',
              '벽, 바닥 색상과 질감 자동 분석',
              '원하는 텍스처로 자유롭게 변경 가능',
              '방 크기(가로·세로·높이) 직접 조정',
            ]}
          />
        </div>
      </section>

      <section className="lp-surface-soft">
        <div className="lp-wrap lp-wrap-wide">
          <h2>실제 축척으로 3D 공간에 배치해요</h2>
          <p className="lp-section-sub">인식된 가구를 실제 비율 그대로 3D 공간에 배치해서 미리 볼 수 있어요.</p>
          <DemoStage
            src="/room-demo-interior3d.mp4"
            title="실제 비율의 3D 배치"
            lead={<>카메라 각도를 기반으로 실측 스케일을 계산해서, <br className="lp-lead-break" />가구를 실제 크기 그대로 배치해요.</>}
            features={[
              '카메라 각도 기반 실측 스케일 적용',
              '벽, 창문, 문 위치를 반영한 배치',
              '기존 가구와 겹치지 않는 자동 배치',
              '자유롭게 회전·이동하며 확인 가능',
            ]}
          />
        </div>
      </section>

      <section className="lp-section-auto">
        <div className="lp-wrap">
          <h2>사진만 있으면 충분해요</h2>
          <p className="lp-section-sub lp-section-sub-nowrap">복잡한 치수 입력이나 도면 없이, 스마트폰으로 찍은 사진 한 장이면 시작할 수 있어요.</p>
          <div className="lp-signature-grid">
            <div className="lp-sig-card lp-coral">
              <h3>내 방 구조를 그대로 인식</h3>
              <p>벽과 창문, 기존 가구 위치까지 사진에서 읽어내서 실제 공간에 맞는 배치를 제안해요.</p>
            </div>
            <div className="lp-sig-card lp-forest">
              <h3>여러 스타일로 비교</h3>
              <p>같은 방이라도 원하는 무드에 따라 몇 가지 배치안을 만들어 나란히 비교할 수 있어요.</p>
            </div>
          </div>
        </div>
      </section>

      <section className="lp-surface-soft lp-section-auto">
        <div className="lp-wrap">
          <h2>세 단계면 끝나요</h2>
          <p className="lp-section-sub">가입도, 도면 작업도 필요 없어요.</p>
          <div className="lp-steps">
            <div className="lp-step-card">
              <div className="lp-step-num">01</div>
              <h4>사진 올리기</h4>
              <p>빈 방을 찍은 사진 한 장을 업로드해요.</p>
            </div>
            <div className="lp-step-card">
              <div className="lp-step-num">02</div>
              <h4>배치 확인하기</h4>
              <p>가구가 배치되는 3D 결과를 바로 확인해요.</p>
            </div>
            <div className="lp-step-card">
              <div className="lp-step-num">03</div>
              <h4>저장하고 공유하기</h4>
              <p>마음에 드는 배치를 저장하거나 링크로 공유해요.</p>
            </div>
          </div>
        </div>
      </section>

      <section id="lp-start" className="lp-section-auto">
        <div className="lp-wrap">
          <div className="lp-cta-band">
            <h2>지금 방 사진을 올려보세요</h2>
            <button className="lp-btn lp-btn-primary" onClick={onStart}>시작하기</button>
          </div>
        </div>
      </section>

      <footer className="lp-footer">
        <div className="lp-wrap">
          <p>MyEmptyRoom</p>
        </div>
      </footer>
    </div>
  )
}
