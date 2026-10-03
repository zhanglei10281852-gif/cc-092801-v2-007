# 红白喜事服务运营平台

这是一个面向婚庆公司、殡葬服务机构和现场调度人员的 Python 后端服务，用于管理服务套餐、家庭订单、现场执行队列、服务人员、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/ceremony-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

服务订单运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。结算版本化接口位于 `/api/settlements`，需要登录会话并按 `settlements.read` / `settlements.write` / `settlements.review` 权限放行。

## 结算版本化流程

仪式结束后补录的礼金、场地加时和供应商实际用量按批次导入结算单，金额一律以“分”存储与返回（请求中以元为单位、最多两位小数）。每个结算单的状态机为：草稿（可反复导入、重算、作废条目）→ 已确认（生成不可变的应付/收款快照与完整性摘要值）→ 已发布（仅 `settlements.review` 权限的复核人可操作）→ 被替代或已撤销。已确认版本不能再导入或重算，修改必须开启新版本，旧版本快照与差异（含两任确认人、发布人）永久保留；撤销同样需要复核权限并记录原因。

- 导入幂等：同一 `batch_key` 重复提交返回首次结果（`replayed=true`），键相同但内容不同返回 409；同一外部单号同金额自动去重，金额不一致标记冲突并保留新旧金额。
- 发布门禁：场地加时与供应商用量缺凭证号、或存在未处理的金额冲突时，发布返回 409 并在 `error.context.issues` 中列出具体条目；冲突需在草稿中通过 `keep_existing` / `accept_incoming` 显式处理。
- 重启安全：所有晋级都在单个即时事务内落盘，服务重启不会把草稿或已确认版本误置为已发布。
- 回放与审计：`GET /api/settlements/{id}/versions/{version}` 返回该版本的快照、条目与导入批次，`GET /api/settlements/{id}/audit` 返回完整审计链。

## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
```

## 目录结构

```text
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/settlement/     结算单、费用批次导入、版本晋级、快照与发布门禁
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
