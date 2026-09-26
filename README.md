# 教材试用观察期

纯 Python 服务端服务，仅依赖标准库与本地 SQLite，提供教材试用案例的
**创建草稿 → 补充证据 → 送审 → 审核 → 签发 → 归档** 最小业务闭环。

- 版本化状态机 + 乐观版本号，非法迁移返回明确原因
- 每次写操作响应均含 `state_change`（from/to、reason、actor、时间），案例视图含完整 `history`
- 送审前强制具备「课标契合度」与「试用反馈」两类证据；驳回后可补证据重新送审
- 证书编号唯一，签发与登记在同一事务，冲突整体回滚
- 创建支持幂等键；支持按状态/申请人/教材/关键字/日期范围检索
- 时间统一 UTC ISO-8601，日期范围按字符串比较，正确处理跨午夜/跨年边界

## 测试

```bash
python3 -m unittest discover -s tests -v   # 24 个测试，含跨日期边界与持久化重启
python3 -m compileall -q service_09261_005 tests
```

## 运行服务

```bash
python3 -m service_09261_005.app --db trial.db --port 8000
```

## API

| 方法 & 路径 | 说明 |
|---|---|
| `POST /cases` | 创建草稿（可带 `idempotency_key`，首次 201，重放 200） |
| `POST /cases/{id}/evidence` | 补充证据（仅 draft/rejected） |
| `POST /cases/{id}/submit` | 送审（证据不全返回 422） |
| `POST /cases/{id}/reviews` | 审核：`approved` + 必填 `reason` |
| `POST /cases/{id}/issuance` | 签发证书（必填 `certificate_no`，默认有效期 180 天） |
| `POST /cases/{id}/archive` | 归档（必填 `reason`） |
| `POST /cases/{id}/cancel` | 撤销草稿（必填 `reason`） |
| `GET /cases/{id}` | 案例详情（证据/证书/历史） |
| `GET /cases` | 条件检索：`state, applicant, textbook, q, date_from, date_to, date_field=created_at|updated_at, limit` |

状态流：

```
draft ──submit──▶ reviewing ──approve──▶ approved ──issue──▶ issued ──archive──▶ archived
  │                  │                                            
  ├──cancel──▶ cancelled         └──reject──▶ rejected ──submit──▶ reviewing（补证据后重审）
```

成功响应示例：

```json
{
  "case": {"id": "c1", "state": "issued", "version": 4, ...},
  "state_change": {
    "from_state": "approved", "to_state": "issued",
    "reason": "签发试用证书 CERT-2026-0001，有效期至 2027-03-25T...",
    "actor": "王签发", "occurred_at": "2026-09-27T00:00:10+00:00"
  },
  "evidence": [...], "issuance": {...}, "history": [...]
}
```

错误响应统一为 `{"error": "<code>", "message": "<原因>"}`，状态码：400 参数错误、
404 案例不存在、409 状态/唯一性冲突、422 业务门槛不满足。
