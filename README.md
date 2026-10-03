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

服务订单运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。费用结算版本化接口位于 `/api/settlements`。

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
app/settlement/     费用结算版本化：草稿计算、确认快照、发布/撤销、幂等导入与审计链
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

## 费用结算版本化

仪式结束后家属补录礼金、场地加时与供应商实际用量时，结算按案件维度进行版本化晋级：

1. `POST /api/settlements/cases` 建立案件并自动生成 v1 草稿；
2. 草稿阶段可反复 `entries/import`（按 `(案件, import_batch)` 幂等，同批次不同数据返回 409）与 `recalculate`，条目按行键更新而不会覆盖历史；
3. `confirm` 生成不可变的应付/收款/净额快照（整数分存储），快照摘要链式包含上一版本摘要；缺凭证、非正金额、用量×单价冲突、同单据金额冲突会逐条列在 422 的 `context.issues` 中阻止晋级；
4. 仅持有 `settlements.publish` 权限（`finance_reviewer` 角色或管理员）的复核人可以 `publish` 或 `revoke`；撤销待发布的新版本时，更早的已发布版本继续生效；
5. 已发布版本不可修改，家属补录需 `new-version` 结转基线生成新草稿；新版本发布时旧版本置为 `revoked`，保留逐项差异（新增/变更/删除、金额增量、合计变化）与替代责任人；
6. 状态全部持久化，服务重启后草稿不会误变成已发布；
7. `GET /cases/{id}/versions/{n}` 可按版本回放明细，`GET /cases/{id}/audit` 提供完整审计链（同时写入全局 `audit_events`）。
