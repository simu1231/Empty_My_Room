#!/usr/bin/env bash
# 단계 7 — API 의 8000 포트를 CloudFront 엣지에게만 연다.
#
# 단계 6 전까지는 브라우저가 이 포트를 직접 불렀으므로 0.0.0.0/0 외에 방법이
# 없었다. 이제 CloudFront 가 앞에 서므로 이 포트를 부르는 건 엣지뿐이다.
# 열어 두면 누구나 CloudFront 를 우회해 /api/prewarm 을 때려 **GPU 를 깨울 수**
# 있다. 돈이 드는 구멍이라 막는다.
#
# 순서가 중요하다. 지우고 넣는 게 아니라 **넣고 지운다**:
#   - 할당량(규칙 60개)에 걸려 추가가 거부되면 아무것도 바뀌지 않은 채 끝난다.
#     먼저 지웠다면 그 순간 API 가 통째로 끊긴 채 거부를 맞는다.
#   - 검증이 실패해도 원복이 "지운 것 하나 되살리기" 한 줄이다.
set -euo pipefail
cd "$(dirname "$0")"
. ./config.sh >/dev/null

DRY_RUN=${DRY_RUN:-1}
PL=${EMR_CF_PREFIX_LIST:?}
PORT=${API_PORT:-8000}
BACKUP=/tmp/emr-sg-backup-$(date +%Y%m%d-%H%M%S).json

say(){ echo "  $*"; }
run(){ if [ "$DRY_RUN" != "0" ]; then echo "    [계획] $*"; else "$@"; fi; }
# run() 은 stdout 을 버리므로 값을 받아야 하는 호출에는 못 쓴다. 계획 모드에서
# 진짜로 리소스를 만들어 버린 적이 있어서(단계 6) 전용 래퍼를 둔다.
run_id(){ local fake="$1"; shift
  if [ "$DRY_RUN" != "0" ]; then echo "    [계획] $*" >&2; echo "$fake"; else "$@"; fi; }

SG_ID=$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=${API_NAME:-emr-api}-sg" \
  --query 'SecurityGroups[0].GroupId' --output text)
[ "$SG_ID" != "None" ] || { echo "보안그룹을 찾지 못했다" >&2; exit 1; }
say "보안그룹 $SG_ID"

# 검증 대상 두 주소는 적어 두지 않고 매번 조회한다. config 에 박아 두면
# 배포를 다시 만든 날 조용히 옛 주소를 검사하게 되고, 그러면 "막혔다"는
# 결론이 실은 "엉뚱한 데를 찔렀다"가 된다.
CF_DOMAIN=${EMR_CF_DOMAIN:-$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='${EMR_CF_COMMENT}'].DomainName|[0]" --output text)}
API_IP=${EMR_API_ORIGIN_IP:-$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=${API_NAME:-emr-api}" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)}
[ -n "$CF_DOMAIN" ] && [ "$CF_DOMAIN" != "None" ] || { echo "CloudFront 도메인을 찾지 못했다" >&2; exit 1; }
[ -n "$API_IP" ] && [ "$API_IP" != "None" ] || { echo "API 공인 IP 를 찾지 못했다" >&2; exit 1; }
say "CloudFront $CF_DOMAIN / API 직통 $API_IP"

# ---------------------------------------------------------------- [1/5] 백업
echo "[1/5] 교체 전 상태 백업"
aws ec2 describe-security-group-rules --filters "Name=group-id,Values=$SG_ID" > "$BACKUP"
say "$BACKUP"

OPEN_RULE=$(python3 - "$BACKUP" "$PORT" <<'PY'
import json,sys
b,port=sys.argv[1],int(sys.argv[2])
for r in json.load(open(b))["SecurityGroupRules"]:
    if not r["IsEgress"] and r.get("CidrIpv4")=="0.0.0.0/0" and r.get("FromPort")==port:
        print(r["SecurityGroupRuleId"]); break
PY
)
if [ -z "$OPEN_RULE" ]; then
  say "$PORT 의 0.0.0.0/0 규칙이 이미 없다 — 할 일 없음"
  exit 0
fi
say "교체 대상 $OPEN_RULE  ($PORT ← 0.0.0.0/0)"

# ------------------------------------------------------- [2/5] 프리픽스 추가
echo "[2/5] CloudFront 프리픽스 리스트 규칙 추가 (먼저 넣는다)"
if [ "$DRY_RUN" != "0" ]; then
  say "[계획] authorize-security-group-ingress $PORT ← $PL"
elif ! aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
      --ip-permissions "IpProtocol=tcp,FromPort=$PORT,ToPort=$PORT,PrefixListIds=[{PrefixListId=$PL,Description='CloudFront origin-facing only'}]" \
      >/dev/null 2>/tmp/emr-lockdown-err; then
  if grep -q InvalidPermission.Duplicate /tmp/emr-lockdown-err; then
    say "이미 있다 — 그대로 둔다"
  elif grep -q RulesPerSecurityGroupLimitExceeded /tmp/emr-lockdown-err; then
    echo "  ✗ 보안그룹 규칙 할당량 초과. 프리픽스 리스트가 MaxEntries 만큼" >&2
    echo "    세어지는데 이 리스트는 AWS 소유라 그 값을 미리 볼 수 없었다." >&2
    echo "    **아무것도 바뀌지 않았다** — 8000 은 그대로 열려 있다." >&2
    echo "    할당량 L-0EA8095F 증설을 요청할지 보고 후 결정한다." >&2
  else
    cat /tmp/emr-lockdown-err >&2
    exit 1
  fi
  [ -s /tmp/emr-lockdown-err ] && grep -q InvalidPermission.Duplicate /tmp/emr-lockdown-err || exit 1
fi
say "추가됨 — 지금은 두 규칙이 공존한다(엣지도 전체도 허용)"

# ------------------------------------------------------- [3/5] 전체공개 제거
echo "[3/5] 0.0.0.0/0 규칙 제거"
run aws ec2 revoke-security-group-ingress --group-id "$SG_ID" \
    --security-group-rule-ids "$OPEN_RULE"
say "제거됨 $OPEN_RULE"

# ------------------------------------------------------------- [4/5] 검증
echo "[4/5] 검증"
rollback(){
  echo "  ✗ 검증 실패 — 즉시 원복한다" >&2
  aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port "$PORT" --cidr 0.0.0.0/0 >/dev/null || true
  echo "  0.0.0.0/0 복구 완료. 프리픽스 규칙은 남겨 둔다(무해)." >&2
  exit 1
}
if [ "$DRY_RUN" != "0" ]; then
  say "[계획] CloudFront 경유 / 와 /api/gpu/status 가 200 인지"
  say "[계획] EIP 직통 http://<EIP>:$PORT/health 가 막혔는지"
else
  CF="https://$CF_DOMAIN"
  for p in "/" "/api/gpu/status"; do
    C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$CF$p" || echo 000)
    say "CloudFront $p → $C"
    [ "$C" = "200" ] || rollback
  done
  # 직통은 **타임아웃이어야** 막힌 증거다. 거부(Connection refused)가 아니라
  # 타임아웃인 이유는 보안그룹이 패킷을 응답 없이 버리기 때문이다.
  # 규칙 제거는 즉시 반영되지 않는다. 실측 약 8초. 한 번만 쏘면 아직 열려
  # 있는 걸 보고 "실패"로 단정해 멀쩡한 작업을 되돌린다 — 실제로 그랬다.
  # 막힘을 확인할 때까지 60초간 기다린다.
  D=200
  for _ in $(seq 1 15); do
    D=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
          "http://$API_IP:$PORT/health" 2>/dev/null) || D=000
    [ "$D" = "000" ] && break
    sleep 2
  done
  if [ "$D" = "000" ]; then
    say "EIP 직통 :$PORT/health → 응답 없음(타임아웃) — 막혔다"
  else
    echo "  ✗ 60초가 지나도 직통이 $D 로 응답한다 — 포트가 안 막혔다" >&2
    rollback
  fi

  # 직통을 막은 뒤에도 CloudFront 가 멀쩡한지 다시 본다. 엣지 대역을 잘못
  # 골랐다면 여기서 드러난다.
  C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$CF/api/gpu/status" || echo 000)
  say "CloudFront /api/gpu/status (잠금 후) → $C"
  [ "$C" = "200" ] || rollback
fi

# ------------------------------------------------------------- [5/5] 요약
echo "[5/5] 완료"
say "$PORT ← $PL (CloudFront 엣지 전용)"
say "원복이 필요하면:"
say "  aws ec2 authorize-security-group-ingress --group-id $SG_ID \\"
say "    --protocol tcp --port $PORT --cidr 0.0.0.0/0"
