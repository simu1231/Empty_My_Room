#!/bin/bash
# AMI를 굽기 위한 준비. **AMI 원본 인스턴스 안에서** 실행한다(로컬 PC 아님).
#
#   1) 도커 이미지를 git SHA 태그로 빌드해 둔다
#   2) 가디언/자가회수 스크립트를 /opt/emr/bin 에 설치하고 systemd 타이머를 켠다
#   3) 스냅샷에 남으면 안 되는 것들을 지운다
#
# 왜 이미지를 미리 굽나 — `docker compose up -d` 는 이미지가 없으면 그 자리에서
# 빌드한다. 부팅 중 빌드는 수십 분이고 그동안 GPU 인스턴스 요금이 계속 나간다.
# 왜 가디언을 여기서 설치하나 — userdata가 설치하면, userdata가 실패했을 때
# 가디언도 없다. 가디언은 userdata 실패를 잡으라고 있는 물건이라 앞뒤가 안 맞는다.
set -euo pipefail
cd "$(dirname "$0")"
. ./config.sh

REPO_ROOT=$(cd ../.. && pwd)
echo "▶ 저장소 $REPO_ROOT / 루트 $EMR_ROOT / 태그 $EMR_IMAGE_TAG"

# ── 0. 경로 점검 ─────────────────────────────────────────────────────────
# 컨테이너는 호스트와 **같은 절대경로**로 모델을 마운트한다. 이 인스턴스의 실제
# 배치가 config.sh의 EMR_ROOT와 어긋나면 컨테이너는 멀쩡히 뜬 뒤 첫 요청에서
# import 에러로 죽는다 — 그때는 이미 AMI를 구운 뒤다. 굽기 전에 깨뜨린다.
[ "$REPO_ROOT" = "$EMR_REPO_DIR" ] || {
  echo "✗ 저장소는 $REPO_ROOT 에 있는데 config.sh는 $EMR_REPO_DIR 를 가리킨다"
  echo "  저장소를 옮기거나 EMR_ROOT 를 맞추세요."; exit 1; }

for d in miniconda3 sam-3d-objects uLayout omni3d detectron2 \
         pytorch3d_omni3d_build sam2_repo lama_repo lama_model \
         .cache/huggingface; do
  [ -d "$EMR_ROOT/$d" ] || { echo "✗ $EMR_ROOT/$d 가 없다 — 설치가 덜 끝났다"; exit 1; }
done

# 컨테이너는 uid 1000으로 돈다(호스트 파일을 uid로 매칭한다). HF 캐시는 락 파일을
# 쓰므로 읽기만으로는 부족하다. root 소유로 깔아두면 첫 추론에서 PermissionError다.
_owner=$(stat -c %u "$EMR_ROOT/.cache/huggingface")
[ "$_owner" = "1000" ] || {
  echo "✗ $EMR_ROOT/.cache/huggingface 소유자가 uid $_owner (uid 1000이어야 한다)"
  echo "  sudo chown -R 1000:1000 $EMR_ROOT"; exit 1; }
unset _owner

# ── 1. 이미지 빌드 ───────────────────────────────────────────────────────
# 컴포즈 파일에 image: 태그를 박아뒀으므로 build 만으로 태그가 붙는다.
# CPU용(emr/worker)과 GPU용(emr/gpu)이 같은 디렉터리를 쓰지만 태그가 달라서
# 서로 덮어쓰지 않는다 — 예전엔 gpu 오버라이드가 build만 바꾸고 image를 물려받아
# CPU 이미지를 GPU 이미지로 덮어쓸 수 있는 상태였다.
export EMR_IMAGE_TAG
( cd "$REPO_ROOT/deploy" && \
  docker compose -f docker-compose.yml -f docker-compose.gpu.yml build )

# ── 2. 호스트 스크립트 설치 ──────────────────────────────────────────────
sudo install -d -m 755 /opt/emr/bin
sudo install -m 755 self-retire.sh /opt/emr/bin/self-retire.sh
sudo install -m 755 guardian.sh    /opt/emr/bin/guardian.sh
# 가디언의 상태 디렉터리를 여기서 만들지 않는다. 이젠 /run/emr (tmpfs) 이라
# 부팅마다 사라지고, guardian.sh 가 돌 때마다 직접 mkdir 한다.

sudo tee /etc/systemd/system/emr-guardian.service >/dev/null <<UNIT
[Unit]
Description=EMR 가디언 — 리퍼가 죽었을 때 인스턴스를 회수해 요금을 끊는다
After=docker.service

[Service]
Type=oneshot
Environment=GUARDIAN_GRACE_SEC=${GUARDIAN_GRACE_SEC}
Environment=GUARDIAN_FAIL_MIN=${GUARDIAN_FAIL_MIN}
ExecStart=/opt/emr/bin/guardian.sh
UNIT

# OnBootSec 를 짧게 둬도 스크립트 안에서 uptime 유예를 보므로 안전하다.
# Persistent=false: 꺼져 있던 동안의 실행을 몰아서 하지 않는다.
sudo tee /etc/systemd/system/emr-guardian.timer >/dev/null <<'UNIT'
[Unit]
Description=EMR 가디언 1분 주기

[Timer]
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=10s
Persistent=false

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable emr-guardian.timer
sudo systemctl start  emr-guardian.timer

# ── 2.5 워밍 목록 사전 생성 ──────────────────────────────────────────────
# 기동 뒤 워밍이 읽을 파일 목록을 **지금** 만들어 AMI 에 넣는다.
#
# 부팅할 때마다 find 로 만들고 있었는데, 그게 혼자 53초를 먹었다(파일 수십만
# 개의 inode 를 지연 로딩으로 긁는다). 백그라운드로 옮기고 io 우선순위를 idle
# 로 깔자 더 느려져서, 워커 9호는 **읽기를 시작도 못 한 채** 유휴 회수됐다.
#
# 그런데 이 목록의 대상은 AMI 안의 불변 데이터다. 부팅마다 다시 세어야 할
# 이유가 없다. 여기서 한 번 세어 두면 워커는 파일을 읽기만 하면 된다.
# (목록이 없는 옛 AMI 로도 떠야 하므로 userdata 쪽에 find 폴백을 남겨 뒀다.)
#
# 예전에는 여기서 "8MB 넘는 파일 전부"를 담아 46GB 목록이 나왔다. 그런데 이
# 기계의 페이지 캐시로 쓸 수 있는 건 7GB 안팎이다(RAM 15GiB - 컨테이너 셋).
# 46GB를 읽으면 뒤에 읽은 게 앞에 읽은 것을 밀어낸다 — 다 읽으면 다 식는다.
# 그래서 세 가지를 더 한다.
#
#   1) EMR_WARM_SKIP 을 **지금** 적용한다. 부팅 쪽 grep 도 그대로 남겨 둔다 —
#      시작 템플릿에서 EMR_WARM_SKIP 만 바꿔 다시 굽지 않고 조정하는 길을
#      막지 않기 위해서다. 여기서 미리 빼는 건 목록을 작게 만들기 위함이다.
#
#   2) 심링크 항목을 따라간다. sam3d 가중치는 checkpoints/hf/*.ckpt 라는
#      **읽을 수 있는 이름**의 심링크이고 실제 파일은 HF 블롭의 해시 이름이다.
#      우선순위를 파일 단위로 적으려면 이름 쪽을 적어야 한다(해시는 모델을
#      다시 받으면 바뀐다). 심링크는 -type f 에 안 걸리므로 따로 받는다.
#
#   3) EMR_WARM_BG_MAX_BYTES 로 총량을 끊는다. EMR_WARM_BG_DIRS 의 **순서가
#      우선순위**다. 예산을 넘기는 파일은 건너뛰고 뒤쪽 작은 파일로 자리를
#      채운다(break 가 아니라 continue 다 — 남은 자리를 비워 둘 이유가 없다).
#
# 같은 파일이 두 경로로 들어올 수 있다(ckpt 심링크와 그 대상인 HF 블롭
# 디렉터리). 실제 경로로 묶어 한 번만 읽는다 — 두 번 읽으면 예산만 먹는다.
# mktemp 로 받는다. 고정 이름(/tmp/emr-warmcand)을 쓰다가 막혔다 — 우분투는
# fs.protected_regular=2 라 sticky 디렉터리(/tmp)에서 **root 가 남의 소유
# 파일을 열지 못한다**. 검증하느라 ubuntu 로 한 번 돌려 둔 찌꺼기가 남아
# 있었고, sudo 로 다시 돌리니 거기서 Permission denied 로 죽었다. 세계
# 쓰기 가능한 디렉터리에 예측 가능한 이름을 만드는 것 자체가 좋지 않다.
WARMLIST=$(mktemp -t emr-warmlist.XXXXXXXX)
CAND=$(mktemp -t emr-warmcand.XXXXXXXX)
SEEN=$(mktemp -t emr-warmseen.XXXXXXXX)
BUDGET=$(numfmt --from=iec "${EMR_WARM_BG_MAX_BYTES:-9G}")

for p in $EMR_WARM_BG_DIRS; do
  if [ -d "$p" ]; then
    find -L "$p" -type f -size +8M ! -name '*.a' -print 2>/dev/null
  elif [ -e "$p" ]; then
    printf '%s\n' "$p"
  else
    echo "  ⚠ $p 없음 — 워밍 목록에서 빠진다" >&2
  fi
done > "$CAND"

if [ -n "${EMR_WARM_SKIP:-}" ]; then
  # grep 이 전부 걸러내면 1을 반환한다 — set -e 에 걸리므로 받아낸다.
  grep -Ev "$(printf '%s\n' $EMR_WARM_SKIP | paste -sd'|' -)" "$CAND" > "$CAND.f" || true
  mv "$CAND.f" "$CAND"
fi

: > "$WARMLIST"
: > "$SEEN"
# 한 줄에 "경로 skip count" 를 적는다. 단위는 dd 가 쓰는 4MiB 블록이다.
# 파일 하나를 통째로 한 줄에 적으면 xargs -P 64 가 **파일 단위**로만 갈라져서,
# 4개짜리 목록에서는 4갈래밖에 안 돈다. 지연 로딩 볼륨은 1갈래 10.5 MB/s,
# 32갈래 70 MB/s 다 — 갈래 수가 곧 속도다. 그래서 큰 파일을 조각내 둔다.
# (경로에 공백이 있으면 깨진다. 모델 경로엔 없고, 예전 목록도 줄 단위였다.)
CH=$(( ${EMR_WARM_BG_CHUNK_MB:-128} / 4 ))
[ "$CH" -gt 0 ] || CH=32
wl_bytes=0 wl_n=0 wl_over=0 wl_c=0
while read -r f; do
  r=$(readlink -f "$f" 2>/dev/null) || continue
  [ -n "$r" ] && [ -f "$r" ] || continue
  grep -Fxq "$r" "$SEEN" && continue
  printf '%s\n' "$r" >> "$SEEN"
  s=$(stat -c %s "$r" 2>/dev/null) || continue
  # 첫 파일은 예산보다 커도 넣는다 — 그래야 예산을 잘못 잡아도 0개가 안 된다.
  if [ "$wl_n" -gt 0 ] && [ $(( wl_bytes + s )) -gt "$BUDGET" ]; then
    wl_over=$(( wl_over + 1 )); continue
  fi
  # 적는 건 $r(해시 이름)이 아니라 $f(읽을 수 있는 심링크 이름)다. 해시는
  # 모델을 다시 받으면 바뀐다. dd 는 심링크를 알아서 따라간다.
  nb=$(( (s + 4194304 - 1) / 4194304 ))   # 4MiB 블록 수(올림)
  k=0
  while [ "$k" -lt "$nb" ]; do
    printf '%s %d %d\n' "$f" "$k" "$CH" >> "$WARMLIST"
    k=$(( k + CH )); wl_c=$(( wl_c + 1 ))
  done
  wl_bytes=$(( wl_bytes + s )) wl_n=$(( wl_n + 1 ))
done < "$CAND"

sudo install -m 644 "$WARMLIST" /opt/emr/warmlist
echo "  워밍 목록 파일 ${wl_n}개 / 조각 ${wl_c}개 / $(numfmt --to=iec "$wl_bytes")B → /opt/emr/warmlist"
echo "    예산 $(numfmt --to=iec "$BUDGET")B, 넘쳐서 제외 ${wl_over}개, 후보 $(wc -l < "$CAND")개"
rm -f "$WARMLIST" "$CAND" "$CAND.f" "$SEEN"

# ── 3. 스냅샷 위생 ───────────────────────────────────────────────────────
# .env 는 인스턴스가 뜰 때 userdata가 다시 만든다. AMI에 남겨두면 옛 큐 이름이
# 굳어버리고, 혹시 자격증명이 들어가면 AMI를 공유하는 순간 같이 나간다.
sudo rm -f "$REPO_ROOT/deploy/.env"
# 인스턴스 역할을 쓰므로 정적 키가 AMI에 있을 이유가 없다.
sudo rm -rf /root/.aws /home/ubuntu/.aws
# cloud-init 상태를 지워야 새 인스턴스에서 userdata가 처음처럼 돈다.
sudo cloud-init clean --logs 2>/dev/null || true
sudo rm -f /var/log/emr-userdata.log
# 예전에는 가디언 실패 카운터가 /var/lib/emr 에 있었고, 여기서 그걸 지우고
# 있었다. 그런데 이 줄 **뒤에** smoke-test.sh 가 돌면서 스택을 내리면 가디언이
# 곧바로 다시 쌓기 시작해서, 스냅샷에는 결국 카운터가 박혔다. 지우는 순서를
# 바꾸는 대신 카운터를 tmpfs 로 옮겨서 고쳤다(guardian.sh 주석 참고).
# 아래는 옛 버전 AMI를 다시 굽는 경우를 위한 뒷정리다.
sudo rm -rf /var/lib/emr

echo "✔ 준비 완료 — 굽기 전에 두 가지를 순서대로 돌린다:"
echo "    ./smoke-test.sh    실제로 도는지 (세 모델에 진짜 작업을 통과시킨다)"
echo "    ./verify-ami.sh    있어야 할 것이 있는지"
echo "  둘 다 통과하면 스냅샷을 찍는다."
