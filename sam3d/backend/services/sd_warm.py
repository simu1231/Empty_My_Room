"""
SD 가중치를 **디스크에서만** 미리 읽어 둔다. GPU 에는 올리지 않는다.

■ 무엇을 고치려는 건가
새로 뜬 워커의 첫 인페인팅에서 SD 로드가 553초 걸린다(CloudWatch 실측
553.15초 / 532.53초, 서로 다른 인스턴스의 첫 호출). 그런데 똑같은 코드가
개발 PC 에서는 **5.30초**에 끝난다. 104배 차이는 역직렬화로 설명되지 않는다.
남는 설명은 하나다 — **첫 읽기가 느리다.**

로그가 그렇게 말한다.

    Loading pipeline components...: 100%|█████| 6/6 [07:52<00:00, 98.28s/it]

`Downloading` 이 아니다. HF 캐시 28GB 는 docker-compose.gpu.yml 의
`x-mounts: &model_mounts` 로 rw 마운트돼 있으니 네트워크가 아니다. 그리고
5.4GB / 472초 = **11.7 MB/s** 인데 gp3 는 250 MB/s 프로비저닝이다
(config.sh EMR_VOLUME_THROUGHPUT). 프로비저닝의 4.7% 는 볼륨 처리량 병목이
아니라 **첫 접촉** 의 서명이다.

■ 그래서 여기서 하는 일
`:8001` 이 준비된 뒤 백그라운드에서 가중치 파일을 통독한다. 사용자는 업로드 →
세그먼트 클릭에 보통 2~4분을 쓰고, 인페인팅은 그 뒤에 온다. 그 시간을 쓴다.
GPU 메모리는 건드리지 않는다 — 상주 결정(residency 에 'sd' 추가)은 VRAM 여유를
실측한 뒤의 별개 문제다.

■ EMR_SD_WARM_DROP — 측정으로 결정해야 하는 한 줄
"첫 읽기가 느린" 이유가 두 가지인데 증거가 엇갈린다.

  (a) EBS 스냅샷 지연 로딩  : 블록을 처음 건드릴 때 S3 에서 가져온다. 한 번
      읽으면 **블록 장치 수준에서** 실체화되므로, 페이지 캐시를 버려도 두 번째
      읽기는 빠르다. → DROP=1 이 맞다. RAM 비용 0.
  (b) 콜드 페이지 캐시      : 이전에 sam3d 첫 건 1230초를 파헤쳤을 때의 결론은
      정반대였다 — "실체화된 볼륨도 캐시만 비우면 1201초 넘는다". 이게 맞으면
      2.7GB 를 **RAM 에 붙잡고 있어야** 한다. → DROP=0, RAM 2.7GB 계상.

둘 중 뭔지 추측으로 고르면, (a)인데 DROP=0 을 쓰면 RAM 2.7GB 를 공짜로 버리고,
(b)인데 DROP=1 을 쓰면 선반입이 아무 효과가 없다. 그래서 고르지 않고 **측정해서**
정한다 — measure() 가 그 측정이다.

기본값은 0(= 캐시 유지)이다. 둘 중 틀렸을 때 더 싼 쪽이기 때문이다. 캐시를
유지해서 손해 보는 건 RAM 2.7GB 지만, 캐시를 버려서 틀리면 553초가 그대로다.
"""
import os
import threading
import time

CHUNK = 8 * 1024 * 1024          # 8MB. 더 키워도 EBS 쪽에서 이득이 없었다.

# 1 이면 통독 후 페이지 캐시를 버린다(POSIX_FADV_DONTNEED). 단계 4 측정 결과로
# 정한다 — 위 머리말 참고.
DROP_CACHE = os.environ.get('EMR_SD_WARM_DROP', '0') == '1'

# 0 이면 선반입을 아예 안 한다. 개발 PC 기본값이 0 인 이유는, 로컬은 NVMe +
# 따뜻한 캐시라 5.3초에 끝나서 선반입이 I/O 만 낭비하기 때문이다.
ENABLED = os.environ.get('EMR_SD_WARM', '0') == '1'


def _weight_files():
    """선반입 대상 파일 목록.

    sd_service.sd_weight_paths() 를 그대로 쓴다. 읽는 파일과 로드하는 파일이
    갈라지면 엉뚱한 걸 따뜻하게 해 놓고 왜 느린지 찾게 된다.
    """
    from services.sd_service import sd_weight_paths
    sd_dir, cn_dir = sd_weight_paths()
    files = []
    for root in (sd_dir, cn_dir):
        if not os.path.isdir(root):
            # 저장소 ID 인 경우. HF 캐시의 blob 을 찾아갈 수도 있지만, 그러면
            # 캐시 레이아웃에 의존하게 된다. 선반입은 로컬 fp16 트리가 있을
            # 때만 하는 것으로 한정한다 — 없으면 그냥 건너뛴다.
            continue
        for dirpath, _, names in os.walk(root):
            for n in names:
                if n.endswith(('.safetensors', '.bin')):
                    files.append(os.path.join(dirpath, n))
    return sorted(files)


def _read_once(path):
    """파일 하나를 통독한다. (바이트, 초) 를 돌려준다."""
    n, t0 = 0, time.time()
    fd = os.open(path, os.O_RDONLY)
    try:
        while True:
            b = os.read(fd, CHUNK)
            if not b:
                break
            n += len(b)
        if DROP_CACHE:
            # O_DIRECT 를 쓰지 않는 이유: 파이썬에서 버퍼 정렬을 직접 맞춰야
            # 하고, 정렬이 틀리면 EINVAL 로 조용히 실패한다. 평범하게 읽은 뒤
            # 버리는 쪽이 같은 목적에 훨씬 덜 깨진다.
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)
    return n, time.time() - t0


def warm(app=None):
    """통독 1회. 끝나면 app.state.sd_warm = True."""
    files = _weight_files()
    if not files:
        print('[sd_warm] 로컬 fp16 트리가 없어 선반입을 건너뜁니다 '
              '(EMR_SD_PATH 미설정 또는 경로 없음)')
        if app is not None:
            app.state.sd_warm = None       # "해당 없음" — False(미완료)와 구분
        return

    total, t0 = 0, time.time()
    print(f'[sd_warm] 선반입 시작 — {len(files)}개 파일, '
          f'캐시 {"해제" if DROP_CACHE else "유지"}')
    for p in files:
        try:
            n, dt = _read_once(p)
            total += n
            mb = n / 1048576
            print(f'[sd_warm]   {mb:7.1f}MB {dt:6.2f}초 '
                  f'({mb / dt if dt > 0 else 0:6.1f} MB/s)  {os.path.basename(p)}')
        except OSError as e:
            # 선반입 실패는 치명적이지 않다. 느려질 뿐이고, 여기서 예외를
            # 올리면 아무 이득 없이 기동이 깨진다.
            print(f'[sd_warm]   건너뜀 {p}: {e}')
    elapsed = time.time() - t0
    print(f'[sd_warm] 선반입 완료 — {total / 1048576:.1f}MB / {elapsed:.1f}초 '
          f'({total / 1048576 / elapsed if elapsed > 0 else 0:.1f} MB/s)')
    if app is not None:
        app.state.sd_warm = True


def start(app):
    """lifespan 에서 부른다. 논블로킹 — 준비 판정을 늦추지 않는다."""
    if not ENABLED:
        app.state.sd_warm = None
        return None
    app.state.sd_warm = False
    t = threading.Thread(target=warm, args=(app,), daemon=True,
                         name='sd-warm')
    t.start()
    return t


def measure():
    """
    단계 4 측정. EBS 실체화인지 콜드 페이지 캐시인지 가른다.

        python -c "from services.sd_warm import measure; measure()"

    1회차  : 첫 접촉. 느리다(둘 중 어느 원인이든).
    2회차  : 캐시 유지 상태에서 재독. 빠른 게 당연하다(대조군).
    3회차  : DONTNEED 로 캐시만 버린 뒤 재독.  ← 여기가 판정이다.

        3회차가 1회차보다 훨씬 빠르면  → EBS 실체화가 원인. DROP=1 로 간다.
        3회차가 1회차와 비슷하면       → 페이지 캐시가 원인. DROP=0 으로 간다.
    """
    global DROP_CACHE
    files = _weight_files()
    if not files:
        print('측정할 파일이 없습니다 (EMR_SD_PATH 확인)')
        return

    def pass_(label, drop):
        global DROP_CACHE
        DROP_CACHE = drop
        total, t0 = 0, time.time()
        for p in files:
            n, _ = _read_once(p)
            total += n
        dt = time.time() - t0
        mb = total / 1048576
        print(f'  {label:34s} {mb:7.1f}MB  {dt:7.2f}초  {mb / dt:7.1f} MB/s')
        return dt

    print(f'▶ SD 선반입 측정 — {len(files)}개 파일')
    t1 = pass_('1회차 (첫 접촉, 캐시 유지)', False)
    t2 = pass_('2회차 (캐시 따뜻함, 대조군)', True)   # 끝에 캐시를 버린다
    t3 = pass_('3회차 (캐시 버린 뒤 재독)', False)    # 판정

    print()
    print(f'  1회차 {t1:.2f}초 / 2회차 {t2:.2f}초 / 3회차 {t3:.2f}초')
    if t1 <= 0:
        print('  판정 불가')
    elif t3 < t1 * 0.5:
        print(f'  → 3회차가 1회차의 {t3 / t1 * 100:.0f}%. **EBS 실체화가 원인**.')
        print('     EMR_SD_WARM_DROP=1 로 간다 (RAM 비용 0).')
    else:
        print(f'  → 3회차가 1회차의 {t3 / t1 * 100:.0f}%. **콜드 페이지 캐시가 원인**.')
        print('     EMR_SD_WARM_DROP=0 으로 간다 (RAM 약 2.7GB 계상).')
