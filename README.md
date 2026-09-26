# 教材试用观察期

纯 Python 服务端基础项目，仅依赖本地 SQLite（标准库 `sqlite3`），提供教材试用
案例的最小业务闭环：创建草稿 → 补充证据 → 审核 → 签发 → 按条件检索。

## 状态机

```
draft ──submit──▶ reviewing ──approve──▶ approved ──issue──▶ issued
                     │
                   reject
                     ▼
                 rejected ──(补充晚于驳回时刻的新证据)──resubmit──▶ reviewing
```

- 每次状态变化写入 `events` 流水，带 `reason`（变化原因）；命令响应同时返回
  `reason`（本次动作原因）与 `state_reason`（当前状态解释）。
- 驳回后重新提交必须有至少一条晚于 `rejected_at` 的新证据，否则 422。
- 所有命令支持 `idempotency_key` 幂等重试；版本号 `version` 随每次迁移递增。

## JSON API（`service_09261_005.api.dispatch`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cases` | 创建草稿 `{id, actor, title?}` |
| POST | `/cases/{id}/evidence` | 补充证据 `{actor, content}` |
| POST | `/cases/{id}/submit` | 提交/重新提交审核 |
| POST | `/cases/{id}/review` | 审核 `{decision: approve\|reject, reason?}` |
| POST | `/cases/{id}/issue` | 签发 |
| GET | `/cases/{id}` | 详情（证据 + 变化流水） |
| GET | `/cases/{id}/history` | 状态变化历史 |
| GET | `/cases` | 检索 `?state=&actor=&q=&date=&updated_date=&date_from=&date_to=` |

日期过滤参数取 UTC ISO 时间戳的日期部分（`YYYY-MM-DD`），`date_from/date_to`
为闭区间。

## 持久化

`SQLiteStore(path)` 使用四张表：`cases`、`evidence`、`events`、`idempotency`。
命令在 `BEGIN IMMEDIATE` 事务内执行，异常回滚。默认 `:memory:`，传入文件路径
即可落盘，跨进程重连可继续检索。

## 测试

```
python3 -m unittest discover -s tests -v
```

测试覆盖：完整闭环、驳回→跨午夜补证据→重新提交→签发、按创建/更新日期与
区间检索（含 `23:59:59` / `00:00:00` 边界归属）、文件级 SQLite 持久化、
幂等重试与冲突。编译检查：

```
python3 -m compileall -q service_09261_005 tests
```
