#!/usr/bin/env bash
# 博物馆展柜温度暴露审定服务 —— curl 调用示例
# 用法：BASE_URL=http://localhost:8000 bash examples/curl-examples.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
api() { # method path [json]
  local method="$1" path="$2" json="${3:-}"
  if [[ -n "$json" ]]; then
    curl -sS -X "$method" "$BASE_URL$path" -H 'Content-Type: application/json' -d "$json"
  else
    curl -sS -X "$method" "$BASE_URL$path"
  fi
  echo
}

echo "== 健康检查 =="
api GET /health

echo "== 1. 创建审定窗口（1 小时；允许 18~22°C；最长连续超限 300s；最大采样间隔 600s）=="
WINDOW=$(api POST /api/v1/windows '{
  "name": "展柜A-2026-10-01",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}')
echo "$WINDOW" | python3 -m json.tool
WID=$(echo "$WINDOW" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "== 2. 提交读数（reading_id + revision）=="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":22.4}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-02","revision":1,"measured_at":"2026-10-01T00:20:00Z","temperature":23.1}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-03","revision":1,"measured_at":"2026-10-01T00:30:00Z","temperature":20.5}' >/dev/null

echo "== 3. 相同 ID + 修订号重复提交（幂等，内容一致）=="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1}'

echo "== 4. 相同 ID + 修订号但温度冲突 -> 409 =="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":99.9}' || true

echo "== 5. 旧修订号 -> 409；更高修订号替换重算 =="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":22.4}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":20.0}' || true
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":2,"measured_at":"2026-10-01T00:10:00Z","temperature":20.0}'

echo "== 5b. 采样仪器补传：批次提交（独立窗口 W2，一次提交多条）=="
W2=$(api POST /api/v1/windows '{
  "name": "展柜B-批次补传",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}')
W2ID=$(echo "$W2" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "-- 整批 created：逐项 effect + 按最终状态重算的审定 --"
api POST "/api/v1/windows/$W2ID/readings/batch" '{
  "readings": [
    {"reading_id":"b-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1},
    {"reading_id":"b-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":20.2},
    {"reading_id":"b-02","revision":1,"measured_at":"2026-10-01T00:20:00Z","temperature":20.3}
  ]
}' | python3 -c "import sys,json;d=json.load(sys.stdin);print('effects=',[e['effect'] for e in d['effects']]);print('verdict=',d['current_evaluation']['verdict'])"

echo "-- 同批重放：全部 idempotent，不产生新修订 --"
api POST "/api/v1/windows/$W2ID/readings/batch" '{
  "readings": [
    {"reading_id":"b-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1},
    {"reading_id":"b-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":20.2}
  ]
}' | python3 -c "import sys,json;print('effects=',[e['effect'] for e in json.load(sys.stdin)['effects']])"

echo "-- 两条读数以更高修订号互换采样时刻（与批次内顺序无关，这里先提 b-00）--"
api POST "/api/v1/windows/$W2ID/readings/batch" '{
  "readings": [
    {"reading_id":"b-00","revision":2,"measured_at":"2026-10-01T00:10:00Z","temperature":20.1},
    {"reading_id":"b-01","revision":2,"measured_at":"2026-10-01T00:00:00Z","temperature":20.2}
  ]
}' | python3 -c "import sys,json;d=json.load(sys.stdin);print('effects=',[(e['reading_id'],e['effect'],e['reading']['measured_at']) for e in d['effects']])"

echo "-- 任一项冲突/无效整批拒收：下面批次含同批重复 reading_id -> 422，状态不变 --"
api POST "/api/v1/windows/$W2ID/readings/batch" '{
  "readings": [
    {"reading_id":"b-09","revision":1,"measured_at":"2026-10-01T00:30:00Z","temperature":20.0},
    {"reading_id":"b-09","revision":2,"measured_at":"2026-10-01T00:40:00Z","temperature":20.0}
  ]
}' || true
echo "-- 批次内旧修订号/内容冲突 -> 409，整批不写入 --"
api POST "/api/v1/windows/$W2ID/readings/batch" '{
  "readings": [
    {"reading_id":"b-08","revision":1,"measured_at":"2026-10-01T00:30:00Z","temperature":20.0},
    {"reading_id":"b-00","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":99.9}
  ]
}' || true

echo "== 6. 补齐整窗读数（间隔 <= 600s；末尾含窗口终点读数作为覆盖终止采样）=="
for min in 40 50 60; do
  if [[ "$min" -eq 60 ]]; then ts="2026-10-01T01:00:00Z"; else ts="2026-10-01T00:$min:00Z"; fi
  api POST "/api/v1/windows/$WID/readings" \
    "{\"reading_id\":\"r-$min\",\"revision\":1,\"measured_at\":\"$ts\",\"temperature\":20.0}" >/dev/null
done

echo "== 7. 查看当前审定（区段 + 最长连续超限 + 结论）=="
api GET "/api/v1/windows/$WID/evaluation" | python3 -m json.tool

echo "== 8. 首次签发（v1）=="
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"首次签发"}' | python3 -m json.tool

echo "== 9. 修订 r-02 为正常值后再次签发（v2；v1 快照不变）=="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-02","revision":2,"measured_at":"2026-10-01T00:20:00Z","temperature":20.2}' >/dev/null
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"修订后重新签发"}' | python3 -m json.tool

echo "== 10. 列出全部证书版本 =="
api GET "/api/v1/windows/$WID/certificates" | python3 -m json.tool

echo "== 11. 修订影响报告：比对 v1/v2 两张已签发证书的不可变快照 =="
# 从版本列表取两个证书主键 ID
mapfile -t CIDS < <(api GET "/api/v1/windows/$WID/certificates" \
  | python3 -c "import sys,json;[print(c['id']) for c in json.load(sys.stdin)]")
OLD_ID="${CIDS[0]}"; NEW_ID="${CIDS[1]}"
api GET "/api/v1/certificates/revision-report?old_certificate_id=$OLD_ID&new_certificate_id=$NEW_ID" \
  | python3 -m json.tool

echo "== 12. 报告拒绝：版本倒序 / 跨窗口 / 同 ID（均 422），不存在 404 =="
api GET "/api/v1/certificates/revision-report?old_certificate_id=$NEW_ID&new_certificate_id=$OLD_ID" || true
api GET "/api/v1/certificates/revision-report?old_certificate_id=$OLD_ID&new_certificate_id=99999" || true
