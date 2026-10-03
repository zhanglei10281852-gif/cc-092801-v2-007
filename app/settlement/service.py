from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS settlement_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_code TEXT NOT NULL UNIQUE,
    family_name TEXT NOT NULL,
    ceremony_type TEXT NOT NULL CHECK(ceremony_type IN ('wedding','funeral')),
    ceremony_date TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','confirmed','published')),
    latest_version INTEGER NOT NULL DEFAULT 1,
    published_version INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlement_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id INTEGER NOT NULL REFERENCES settlement_orders(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','confirmed','published','superseded','revoked')),
    calculation_count INTEGER NOT NULL DEFAULT 0,
    payable_total_cents INTEGER NOT NULL DEFAULT 0,
    receivable_total_cents INTEGER NOT NULL DEFAULT 0,
    snapshot_json TEXT,
    snapshot_digest TEXT,
    issues_json TEXT NOT NULL DEFAULT '[]',
    confirmed_by TEXT,
    confirmed_at TEXT,
    published_by TEXT,
    published_at TEXT,
    revoked_by TEXT,
    revoked_at TEXT,
    revoke_reason TEXT,
    superseded_by_version INTEGER,
    superseded_at TEXT,
    diff_from_previous_json TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(settlement_id, version)
);

CREATE TABLE IF NOT EXISTS settlement_import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id INTEGER NOT NULL REFERENCES settlement_orders(id) ON DELETE CASCADE,
    version_id INTEGER NOT NULL REFERENCES settlement_versions(id) ON DELETE CASCADE,
    batch_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    imported_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(settlement_id, batch_key)
);

CREATE TABLE IF NOT EXISTS settlement_fee_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id INTEGER NOT NULL REFERENCES settlement_orders(id) ON DELETE CASCADE,
    version_id INTEGER NOT NULL REFERENCES settlement_versions(id) ON DELETE CASCADE,
    import_batch_id INTEGER REFERENCES settlement_import_batches(id),
    external_ref TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('receivable','payable')),
    category TEXT NOT NULL CHECK(category IN ('gift_money','venue_overtime','supplier_usage','package_fee','other')),
    description TEXT NOT NULL DEFAULT '',
    quantity TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL DEFAULT '',
    unit_price_cents INTEGER,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    voucher_no TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','voided')),
    conflict_flag INTEGER NOT NULL DEFAULT 0 CHECK(conflict_flag IN (0,1)),
    conflict_detail_json TEXT NOT NULL DEFAULT '{}',
    voided_by TEXT,
    voided_at TEXT,
    void_reason TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(version_id, external_ref)
);
CREATE INDEX IF NOT EXISTS idx_settlement_items_version ON settlement_fee_items(version_id, status);

CREATE TABLE IF NOT EXISTS settlement_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id INTEGER NOT NULL REFERENCES settlement_orders(id) ON DELETE CASCADE,
    version_id INTEGER REFERENCES settlement_versions(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settlement_events_order ON settlement_events(settlement_id, id);
"""

# 礼金属于收款，场地加时与供应商实际用量属于应付；套餐与其他费用方向不限。
CATEGORY_DIRECTIONS = {
    "gift_money": "receivable",
    "venue_overtime": "payable",
    "supplier_usage": "payable",
}
# 这两类费用必须提供凭证号才允许发布。
VOUCHER_REQUIRED_CATEGORIES = frozenset({"venue_overtime", "supplier_usage"})


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def to_cents(value: Decimal) -> int:
    return int(value * 100)


class SettlementService:
    """结算单、费用导入、版本晋级（草稿→确认→发布→替代/撤销）的事务服务。

    所有状态晋级都在单个即时事务内完成并落盘，服务重启不会把未完成的
    结算误置为已发布；确认后的快照只读，任何修改都通过新版本显式进行。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_settlements(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        if status:
            rows = self.connection.execute(
                "SELECT * FROM settlement_orders WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
            ).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM settlement_orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get_settlement(self, settlement_id: int) -> dict[str, Any]:
        row = self._require_settlement(self.connection, settlement_id)
        result = dict(row)
        versions = self.connection.execute(
            "SELECT * FROM settlement_versions WHERE settlement_id=? ORDER BY version", (settlement_id,)
        ).fetchall()
        result["versions"] = [self._version_summary(version) for version in versions]
        return result

    def get_version(self, settlement_id: int, version: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM settlement_versions WHERE settlement_id=? AND version=?", (settlement_id, version)
        ).fetchone()
        if row is None:
            raise NotFoundError("结算版本不存在")
        result = self._version_summary(row)
        result["settlement_id"] = settlement_id
        result["snapshot"] = json.loads(row["snapshot_json"]) if row["snapshot_json"] else None
        result["snapshot_digest"] = row["snapshot_digest"]
        result["diff_from_previous"] = json.loads(row["diff_from_previous_json"]) if row["diff_from_previous_json"] else None
        result["issues"] = self._issues_for_version(self.connection, row["id"])
        items = self.connection.execute(
            "SELECT * FROM settlement_fee_items WHERE version_id=? ORDER BY id", (row["id"],)
        ).fetchall()
        result["items"] = [self._item_view(item) for item in items]
        batches = self.connection.execute(
            "SELECT id,batch_key,version_id,imported_by,created_at FROM settlement_import_batches WHERE version_id=? ORDER BY id",
            (row["id"],),
        ).fetchall()
        result["imports"] = [dict(batch) for batch in batches]
        return result

    def audit_trail(self, settlement_id: int) -> list[dict[str, Any]]:
        self._require_settlement(self.connection, settlement_id)
        rows = self.connection.execute(
            "SELECT * FROM settlement_events WHERE settlement_id=? ORDER BY id", (settlement_id,)
        ).fetchall()
        return [
            {
                "id": row["id"],
                "settlement_id": row["settlement_id"],
                "version_id": row["version_id"],
                "action": row["action"],
                "actor": row["actor"],
                "detail": json.loads(row["detail_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 建单与草稿维护
    # ------------------------------------------------------------------
    def create_settlement(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            if connection.execute("SELECT id FROM settlement_orders WHERE order_code=?", (payload["order_code"],)).fetchone():
                raise ConflictError("结算订单号已存在")
            cursor = connection.execute(
                "INSERT INTO settlement_orders(order_code,family_name,ceremony_type,ceremony_date,currency,status,latest_version,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'draft',1,?,?,?)",
                (payload["order_code"], payload["family_name"], payload["ceremony_type"], payload["ceremony_date"], payload["currency"], actor, now, now),
            )
            settlement_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO settlement_versions(settlement_id,version,status,created_by,created_at,updated_at) VALUES(?,1,'draft',?,?,?)",
                (settlement_id, actor, now, now),
            )
            self._event(connection, settlement_id, None, "settlement.create", actor, {
                "order_code": payload["order_code"],
                "family_name": payload["family_name"],
                "ceremony_type": payload["ceremony_type"],
                "ceremony_date": payload["ceremony_date"],
            }, now)
            return self.get_settlement(settlement_id)

    def import_batch(self, settlement_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        items = [self._normalize_item(item) for item in payload["items"]]
        request_hash = digest({"batch_key": payload["batch_key"], "items": sorted(items, key=lambda item: item["external_ref"])})
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            settlement = self._require_settlement(connection, settlement_id)
            existing_batch = connection.execute(
                "SELECT * FROM settlement_import_batches WHERE settlement_id=? AND batch_key=?",
                (settlement_id, payload["batch_key"]),
            ).fetchone()
            if existing_batch is not None:
                if existing_batch["request_hash"] != request_hash:
                    raise ConflictError("同一导入批次键对应了不同的费用内容")
                replayed = json.loads(existing_batch["result_json"])
                replayed["replayed"] = True
                return replayed
            draft = self._require_current_draft(connection, settlement)
            cursor = connection.execute(
                "INSERT INTO settlement_import_batches(settlement_id,version_id,batch_key,request_hash,result_json,imported_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (settlement_id, draft["id"], payload["batch_key"], request_hash, "{}", actor, now),
            )
            batch_id = int(cursor.lastrowid)
            inserted: list[dict[str, Any]] = []
            duplicates: list[dict[str, Any]] = []
            updated: list[dict[str, Any]] = []
            conflicts: list[dict[str, Any]] = []
            for item in items:
                existing_item = connection.execute(
                    "SELECT * FROM settlement_fee_items WHERE version_id=? AND external_ref=?",
                    (draft["id"], item["external_ref"]),
                ).fetchone()
                if existing_item is None:
                    item_cursor = connection.execute(
                        "INSERT INTO settlement_fee_items(settlement_id,version_id,import_batch_id,external_ref,direction,category,description,quantity,unit,unit_price_cents,amount_cents,voucher_no,status,created_by,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)",
                        (
                            settlement_id, draft["id"], batch_id, item["external_ref"], item["direction"], item["category"],
                            item["description"], item["quantity"], item["unit"], item["unit_price_cents"], item["amount_cents"],
                            item["voucher_no"], actor, now, now,
                        ),
                    )
                    inserted.append({"item_id": int(item_cursor.lastrowid), "external_ref": item["external_ref"], "amount_cents": item["amount_cents"]})
                elif existing_item["status"] == "voided":
                    duplicates.append({"item_id": existing_item["id"], "external_ref": item["external_ref"], "note": "条目已作废，保持作废状态"})
                elif int(existing_item["amount_cents"]) == item["amount_cents"]:
                    if not existing_item["voucher_no"] and item["voucher_no"]:
                        connection.execute(
                            "UPDATE settlement_fee_items SET voucher_no=?,updated_at=? WHERE id=?",
                            (item["voucher_no"], now, existing_item["id"]),
                        )
                        updated.append({"item_id": existing_item["id"], "external_ref": item["external_ref"], "voucher_no": item["voucher_no"]})
                    else:
                        duplicates.append({"item_id": existing_item["id"], "external_ref": item["external_ref"], "note": "金额一致，忽略重复导入"})
                else:
                    detail = {
                        "incoming_amount_cents": item["amount_cents"],
                        "incoming_voucher_no": item["voucher_no"],
                        "incoming_batch_key": payload["batch_key"],
                        "incoming_description": item["description"],
                        "recorded_at": now,
                    }
                    connection.execute(
                        "UPDATE settlement_fee_items SET conflict_flag=1,conflict_detail_json=?,updated_at=? WHERE id=?",
                        (json.dumps(detail, ensure_ascii=False, sort_keys=True), now, existing_item["id"]),
                    )
                    conflicts.append({
                        "item_id": existing_item["id"],
                        "external_ref": item["external_ref"],
                        "existing_amount_cents": int(existing_item["amount_cents"]),
                        "incoming_amount_cents": item["amount_cents"],
                    })
            payable, receivable = self._recompute_totals(connection, draft["id"], now)
            result = {
                "settlement_id": settlement_id,
                "batch_key": payload["batch_key"],
                "draft_version": draft["version"],
                "replayed": False,
                "inserted": inserted,
                "duplicates": duplicates,
                "updated": updated,
                "conflicts": conflicts,
                "payable_total_cents": payable,
                "receivable_total_cents": receivable,
            }
            connection.execute(
                "UPDATE settlement_import_batches SET result_json=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False, sort_keys=True), batch_id),
            )
            self._event(connection, settlement_id, draft["id"], "batch.import", actor, {
                "batch_key": payload["batch_key"],
                "inserted": len(inserted),
                "duplicated": len(duplicates),
                "updated": len(updated),
                "conflicts": len(conflicts),
            }, now)
            return result

    def recalculate(self, settlement_id: int, actor: str, note: str = "") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            settlement = self._require_settlement(connection, settlement_id)
            draft = self._require_current_draft(connection, settlement)
            payable, receivable = self._recompute_totals(connection, draft["id"], now)
            connection.execute("UPDATE settlement_versions SET calculation_count=calculation_count+1 WHERE id=?", (draft["id"],))
            self._event(connection, settlement_id, draft["id"], "version.recalculate", actor, {
                "version": draft["version"],
                "note": note,
                "payable_total_cents": payable,
                "receivable_total_cents": receivable,
            }, now)
            return self.get_version(settlement_id, draft["version"])

    def void_item(self, settlement_id: int, item_id: int, reason: str, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._require_settlement(connection, settlement_id)
            item, _version = self._require_draft_item(connection, settlement_id, item_id)
            if item["status"] != "active":
                raise ConflictError("费用条目已经作废")
            connection.execute(
                "UPDATE settlement_fee_items SET status='voided',conflict_flag=0,voided_by=?,voided_at=?,void_reason=?,updated_at=? WHERE id=?",
                (actor, now, reason, now, item_id),
            )
            self._recompute_totals(connection, item["version_id"], now)
            self._event(connection, settlement_id, item["version_id"], "item.void", actor, {
                "item_id": item_id,
                "external_ref": item["external_ref"],
                "amount_cents": int(item["amount_cents"]),
                "reason": reason,
            }, now)
            return self._item_view(connection.execute("SELECT * FROM settlement_fee_items WHERE id=?", (item_id,)).fetchone())

    def resolve_conflict(self, settlement_id: int, item_id: int, action: str, reason: str, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._require_settlement(connection, settlement_id)
            item, _version = self._require_draft_item(connection, settlement_id, item_id)
            if not item["conflict_flag"]:
                raise ConflictError("该费用条目没有待处理的金额冲突")
            detail = json.loads(item["conflict_detail_json"])
            before_amount = int(item["amount_cents"])
            if action == "accept_incoming":
                after_amount = int(detail["incoming_amount_cents"])
                voucher = detail.get("incoming_voucher_no") or item["voucher_no"]
                connection.execute(
                    "UPDATE settlement_fee_items SET amount_cents=?,voucher_no=?,conflict_flag=0,conflict_detail_json='{}',updated_at=? WHERE id=?",
                    (after_amount, voucher, now, item_id),
                )
            else:
                after_amount = before_amount
                connection.execute(
                    "UPDATE settlement_fee_items SET conflict_flag=0,conflict_detail_json='{}',updated_at=? WHERE id=?",
                    (now, item_id),
                )
            self._recompute_totals(connection, item["version_id"], now)
            self._event(connection, settlement_id, item["version_id"], "item.resolve_conflict", actor, {
                "item_id": item_id,
                "external_ref": item["external_ref"],
                "action": action,
                "reason": reason,
                "before_amount_cents": before_amount,
                "after_amount_cents": after_amount,
            }, now)
            return self._item_view(connection.execute("SELECT * FROM settlement_fee_items WHERE id=?", (item_id,)).fetchone())

    # ------------------------------------------------------------------
    # 版本晋级
    # ------------------------------------------------------------------
    def confirm(self, settlement_id: int, actor: str, note: str = "") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            settlement = self._require_settlement(connection, settlement_id)
            draft = self._require_current_draft(connection, settlement)
            items = connection.execute(
                "SELECT * FROM settlement_fee_items WHERE version_id=? AND status='active' ORDER BY id", (draft["id"],)
            ).fetchall()
            snapshot = self._build_snapshot(items, settlement, draft, actor, now)
            issues = self._issues_for_version(connection, draft["id"])
            connection.execute(
                "UPDATE settlement_versions SET status='confirmed',snapshot_json=?,snapshot_digest=?,issues_json=?,"
                "confirmed_by=?,confirmed_at=?,calculation_count=calculation_count+1,updated_at=? WHERE id=?",
                (
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    digest(snapshot),
                    json.dumps(issues, ensure_ascii=False),
                    actor, now, now, draft["id"],
                ),
            )
            self._refresh_settlement_status(connection, settlement_id, now)
            self._event(connection, settlement_id, draft["id"], "version.confirm", actor, {
                "version": draft["version"],
                "note": note,
                "payable_total_cents": snapshot["totals"]["payable_cents"],
                "receivable_total_cents": snapshot["totals"]["receivable_cents"],
                "issue_count": len(issues),
                "snapshot_digest": digest(snapshot),
            }, now)
            return self.get_version(settlement_id, draft["version"])

    def create_version(self, settlement_id: int, actor: str, note: str = "") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            settlement = self._require_settlement(connection, settlement_id)
            latest = connection.execute(
                "SELECT * FROM settlement_versions WHERE settlement_id=? AND version=?",
                (settlement_id, settlement["latest_version"]),
            ).fetchone()
            if latest["status"] == "draft":
                raise ConflictError("当前已存在未确认的草稿版本")
            new_version = int(latest["version"]) + 1
            cursor = connection.execute(
                "INSERT INTO settlement_versions(settlement_id,version,status,created_by,created_at,updated_at) VALUES(?,?,'draft',?,?,?)",
                (settlement_id, new_version, actor, now, now),
            )
            new_version_id = int(cursor.lastrowid)
            carried = connection.execute(
                "SELECT * FROM settlement_fee_items WHERE version_id=? AND status='active' ORDER BY id", (latest["id"],)
            ).fetchall()
            for item in carried:
                connection.execute(
                    "INSERT INTO settlement_fee_items(settlement_id,version_id,import_batch_id,external_ref,direction,category,description,quantity,unit,unit_price_cents,amount_cents,voucher_no,status,conflict_flag,conflict_detail_json,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?)",
                    (
                        settlement_id, new_version_id, item["import_batch_id"], item["external_ref"], item["direction"], item["category"],
                        item["description"], item["quantity"], item["unit"], item["unit_price_cents"], item["amount_cents"],
                        item["voucher_no"], item["conflict_flag"], item["conflict_detail_json"], actor, now, now,
                    ),
                )
            self._recompute_totals(connection, new_version_id, now)
            connection.execute(
                "UPDATE settlement_orders SET latest_version=?,updated_at=? WHERE id=?",
                (new_version, now, settlement_id),
            )
            self._refresh_settlement_status(connection, settlement_id, now)
            self._event(connection, settlement_id, new_version_id, "version.create", actor, {
                "version": new_version,
                "from_version": latest["version"],
                "carried_items": len(carried),
                "note": note,
            }, now)
            return self.get_version(settlement_id, new_version)

    def publish(self, settlement_id: int, actor: str, version: int | None = None, note: str = "") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            settlement = self._require_settlement(connection, settlement_id)
            target_version = version if version is not None else int(settlement["latest_version"])
            row = connection.execute(
                "SELECT * FROM settlement_versions WHERE settlement_id=? AND version=?",
                (settlement_id, target_version),
            ).fetchone()
            if row is None:
                raise NotFoundError("结算版本不存在")
            if target_version != int(settlement["latest_version"]):
                raise ConflictError("只能发布最新版本的结算")
            if row["status"] != "confirmed":
                raise ConflictError("只有已确认的结算版本才能发布")
            snapshot = json.loads(row["snapshot_json"])
            if digest(snapshot) != row["snapshot_digest"]:
                raise ConflictError("结算快照完整性校验失败，禁止发布")
            issues = self._issues_for_version(connection, row["id"])
            if issues:
                raise ConflictError("存在缺失凭证或金额冲突的条目，禁止发布", context={"issues": issues})
            previous = None
            if settlement["published_version"] is not None:
                previous = connection.execute(
                    "SELECT * FROM settlement_versions WHERE settlement_id=? AND version=?",
                    (settlement_id, settlement["published_version"]),
                ).fetchone()
            diff = None
            if previous is not None:
                diff = self._build_diff(previous, json.loads(previous["snapshot_json"]), row, snapshot, actor, now)
                connection.execute(
                    "UPDATE settlement_versions SET status='superseded',superseded_by_version=?,superseded_at=?,updated_at=? WHERE id=?",
                    (target_version, now, now, previous["id"]),
                )
                self._event(connection, settlement_id, previous["id"], "version.supersede", actor, {
                    "previous_version": previous["version"],
                    "current_version": target_version,
                    "diff": diff,
                }, now)
            connection.execute(
                "UPDATE settlement_versions SET status='published',published_by=?,published_at=?,diff_from_previous_json=?,updated_at=? WHERE id=?",
                (actor, now, json.dumps(diff, ensure_ascii=False, sort_keys=True) if diff else None, now, row["id"]),
            )
            connection.execute(
                "UPDATE settlement_orders SET published_version=?,updated_at=? WHERE id=?",
                (target_version, now, settlement_id),
            )
            self._refresh_settlement_status(connection, settlement_id, now)
            self._event(connection, settlement_id, row["id"], "version.publish", actor, {
                "version": target_version,
                "note": note,
                "replaces_version": previous["version"] if previous else None,
                "diff": diff,
            }, now)
            return self.get_version(settlement_id, target_version)

    def revoke(self, settlement_id: int, reason: str, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            settlement = self._require_settlement(connection, settlement_id)
            if settlement["published_version"] is None:
                raise ConflictError("当前结算没有已发布版本，无法撤销")
            row = connection.execute(
                "SELECT * FROM settlement_versions WHERE settlement_id=? AND version=?",
                (settlement_id, settlement["published_version"]),
            ).fetchone()
            if row is None or row["status"] != "published":
                raise ConflictError("已发布版本状态异常，无法撤销")
            connection.execute(
                "UPDATE settlement_versions SET status='revoked',revoked_by=?,revoked_at=?,revoke_reason=?,updated_at=? WHERE id=?",
                (actor, now, reason, now, row["id"]),
            )
            connection.execute(
                "UPDATE settlement_orders SET published_version=NULL,updated_at=? WHERE id=?",
                (now, settlement_id),
            )
            self._refresh_settlement_status(connection, settlement_id, now)
            self._event(connection, settlement_id, row["id"], "version.revoke", actor, {
                "version": row["version"],
                "reason": reason,
            }, now)
            return self.get_version(settlement_id, row["version"])

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _require_settlement(connection: sqlite3.Connection, settlement_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM settlement_orders WHERE id=?", (settlement_id,)).fetchone()
        if row is None:
            raise NotFoundError("结算单不存在")
        return row

    @staticmethod
    def _require_current_draft(connection: sqlite3.Connection, settlement: sqlite3.Row) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM settlement_versions WHERE settlement_id=? AND version=?",
            (settlement["id"], settlement["latest_version"]),
        ).fetchone()
        if row is None or row["status"] != "draft":
            raise ConflictError("当前没有可编辑的草稿版本，请先创建新版本")
        return row

    def _require_draft_item(self, connection: sqlite3.Connection, settlement_id: int, item_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
        item = connection.execute(
            "SELECT * FROM settlement_fee_items WHERE id=? AND settlement_id=?", (item_id, settlement_id)
        ).fetchone()
        if item is None:
            raise NotFoundError("费用条目不存在")
        version = connection.execute("SELECT * FROM settlement_versions WHERE id=?", (item["version_id"],)).fetchone()
        if version["status"] != "draft":
            raise ConflictError("只有草稿版本的费用条目可以处理")
        return item, version

    @staticmethod
    def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
        external_ref = str(item["external_ref"]).strip()
        if not external_ref:
            raise ValidationError("费用条目的外部单号不能为空")
        expected = CATEGORY_DIRECTIONS.get(item["category"])
        if expected is not None and item["direction"] != expected:
            raise ValidationError(f"费用类别 {item['category']} 与收支方向 {item['direction']} 不匹配")
        voucher = (item.get("voucher_no") or "").strip() or None
        quantity = item.get("quantity")
        unit_price = item.get("unit_price")
        return {
            "external_ref": external_ref,
            "direction": item["direction"],
            "category": item["category"],
            "description": (item.get("description") or "").strip(),
            "quantity": "" if quantity is None else str(quantity),
            "unit": (item.get("unit") or "").strip(),
            "unit_price_cents": None if unit_price is None else to_cents(unit_price),
            "amount_cents": to_cents(item["amount"]),
            "voucher_no": voucher,
        }

    @staticmethod
    def _recompute_totals(connection: sqlite3.Connection, version_id: int, now: str) -> tuple[int, int]:
        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN direction='payable' THEN amount_cents ELSE 0 END),0) AS payable,"
            "COALESCE(SUM(CASE WHEN direction='receivable' THEN amount_cents ELSE 0 END),0) AS receivable "
            "FROM settlement_fee_items WHERE version_id=? AND status='active'",
            (version_id,),
        ).fetchone()
        payable, receivable = int(row["payable"]), int(row["receivable"])
        connection.execute(
            "UPDATE settlement_versions SET payable_total_cents=?,receivable_total_cents=?,updated_at=? WHERE id=?",
            (payable, receivable, now, version_id),
        )
        return payable, receivable

    def _issues_for_version(self, connection: sqlite3.Connection, version_id: int) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        rows = connection.execute(
            "SELECT * FROM settlement_fee_items WHERE version_id=? AND status='active' ORDER BY id", (version_id,)
        ).fetchall()
        for item in rows:
            if item["conflict_flag"]:
                detail = json.loads(item["conflict_detail_json"])
                issues.append({
                    "item_id": item["id"],
                    "external_ref": item["external_ref"],
                    "issue": "amount_conflict",
                    "existing_amount_cents": int(item["amount_cents"]),
                    "incoming_amount_cents": detail.get("incoming_amount_cents"),
                    "message": f"条目 {item['external_ref']} 金额冲突：已入账 {item['amount_cents']} 分，新导入 {detail.get('incoming_amount_cents')} 分",
                })
            if item["category"] in VOUCHER_REQUIRED_CATEGORIES and not item["voucher_no"]:
                issues.append({
                    "item_id": item["id"],
                    "external_ref": item["external_ref"],
                    "issue": "missing_voucher",
                    "category": item["category"],
                    "message": f"条目 {item['external_ref']}（{item['category']}）缺少凭证号",
                })
        return issues

    @staticmethod
    def _snapshot_line(item: sqlite3.Row) -> dict[str, Any]:
        return {
            "item_id": item["id"],
            "external_ref": item["external_ref"],
            "category": item["category"],
            "description": item["description"],
            "quantity": item["quantity"],
            "unit": item["unit"],
            "unit_price_cents": item["unit_price_cents"],
            "amount_cents": int(item["amount_cents"]),
            "voucher_no": item["voucher_no"],
        }

    def _build_snapshot(self, items: list[sqlite3.Row], settlement: sqlite3.Row, draft: sqlite3.Row, actor: str, now: str) -> dict[str, Any]:
        def section(rows: list[sqlite3.Row]) -> dict[str, Any]:
            categories: dict[str, dict[str, Any]] = {}
            for item in rows:
                entry = categories.setdefault(item["category"], {"category": item["category"], "amount_cents": 0, "item_count": 0})
                entry["amount_cents"] += int(item["amount_cents"])
                entry["item_count"] += 1
            return {
                "total_cents": sum(int(item["amount_cents"]) for item in rows),
                "categories": [categories[key] for key in sorted(categories)],
                "items": [self._snapshot_line(item) for item in rows],
            }

        payables = section([item for item in items if item["direction"] == "payable"])
        receivables = section([item for item in items if item["direction"] == "receivable"])
        return {
            "currency": settlement["currency"],
            "settlement_id": settlement["id"],
            "order_code": settlement["order_code"],
            "version": draft["version"],
            "generated_by": actor,
            "generated_at": now,
            "item_count": len(items),
            "payables": payables,
            "receivables": receivables,
            "totals": {
                "payable_cents": payables["total_cents"],
                "receivable_cents": receivables["total_cents"],
                "net_cents": receivables["total_cents"] - payables["total_cents"],
            },
        }

    @staticmethod
    def _build_diff(previous_row: sqlite3.Row, previous_snapshot: dict[str, Any], current_row: sqlite3.Row, current_snapshot: dict[str, Any], actor: str, now: str) -> dict[str, Any]:
        def category_map(snapshot: dict[str, Any], section: str) -> dict[str, int]:
            return {entry["category"]: int(entry["amount_cents"]) for entry in snapshot[section]["categories"]}

        deltas: list[dict[str, Any]] = []
        for direction, section in (("payable", "payables"), ("receivable", "receivables")):
            before = category_map(previous_snapshot, section)
            after = category_map(current_snapshot, section)
            for category in sorted(set(before) | set(after)):
                deltas.append({
                    "direction": direction,
                    "category": category,
                    "previous_cents": before.get(category, 0),
                    "current_cents": after.get(category, 0),
                    "delta_cents": after.get(category, 0) - before.get(category, 0),
                })
        return {
            "previous_version": previous_row["version"],
            "current_version": current_row["version"],
            "responsible": {
                "previous_confirmed_by": previous_row["confirmed_by"],
                "previous_published_by": previous_row["published_by"],
                "previous_published_at": previous_row["published_at"],
                "confirmed_by": current_row["confirmed_by"],
                "published_by": actor,
                "published_at": now,
            },
            "payable_delta_cents": current_snapshot["totals"]["payable_cents"] - previous_snapshot["totals"]["payable_cents"],
            "receivable_delta_cents": current_snapshot["totals"]["receivable_cents"] - previous_snapshot["totals"]["receivable_cents"],
            "net_delta_cents": current_snapshot["totals"]["net_cents"] - previous_snapshot["totals"]["net_cents"],
            "category_deltas": deltas,
        }

    @staticmethod
    def _refresh_settlement_status(connection: sqlite3.Connection, settlement_id: int, now: str) -> None:
        settlement = connection.execute(
            "SELECT published_version,latest_version FROM settlement_orders WHERE id=?", (settlement_id,)
        ).fetchone()
        if settlement["published_version"] is not None:
            status = "published"
        else:
            latest = connection.execute(
                "SELECT status FROM settlement_versions WHERE settlement_id=? AND version=?",
                (settlement_id, settlement["latest_version"]),
            ).fetchone()
            status = "confirmed" if latest is not None and latest["status"] == "confirmed" else "draft"
        connection.execute("UPDATE settlement_orders SET status=?,updated_at=? WHERE id=?", (status, now, settlement_id))

    @staticmethod
    def _version_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "version": row["version"],
            "status": row["status"],
            "payable_total_cents": row["payable_total_cents"],
            "receivable_total_cents": row["receivable_total_cents"],
            "calculation_count": row["calculation_count"],
            "confirmed_by": row["confirmed_by"],
            "confirmed_at": row["confirmed_at"],
            "published_by": row["published_by"],
            "published_at": row["published_at"],
            "revoked_by": row["revoked_by"],
            "revoked_at": row["revoked_at"],
            "revoke_reason": row["revoke_reason"],
            "superseded_by_version": row["superseded_by_version"],
            "superseded_at": row["superseded_at"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _item_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "version_id": row["version_id"],
            "import_batch_id": row["import_batch_id"],
            "external_ref": row["external_ref"],
            "direction": row["direction"],
            "category": row["category"],
            "description": row["description"],
            "quantity": row["quantity"],
            "unit": row["unit"],
            "unit_price_cents": row["unit_price_cents"],
            "amount_cents": row["amount_cents"],
            "voucher_no": row["voucher_no"],
            "status": row["status"],
            "conflict_flag": bool(row["conflict_flag"]),
            "conflict_detail": json.loads(row["conflict_detail_json"]),
            "voided_by": row["voided_by"],
            "voided_at": row["voided_at"],
            "void_reason": row["void_reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _event(connection: sqlite3.Connection, settlement_id: int, version_id: int | None, action: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO settlement_events(settlement_id,version_id,action,actor,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (settlement_id, version_id, action, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )
