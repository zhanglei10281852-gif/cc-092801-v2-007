from __future__ import annotations

import json
import sqlite3
from typing import Any


class SettlementRepository:
    """封装结算版本化领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 案件 ----

    def create_case(self, *, case_code: str, ceremony_type: str, family_contact: str, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO settlement_cases(case_code,ceremony_type,family_contact,status,created_by,created_at,updated_at) VALUES(?,?,?, 'draft',?,?,?)",
            (case_code, ceremony_type, family_contact, created_by, now, now),
        )
        return dict(self.require_case(int(cursor.lastrowid)))

    def case_by_id(self, case_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM settlement_cases WHERE id=?", (case_id,)).fetchone()

    def case_by_code(self, case_code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM settlement_cases WHERE case_code=?", (case_code,)).fetchone()

    def require_case(self, case_id: int) -> sqlite3.Row:
        row = self.case_by_id(case_id)
        if row is None:
            from app.core.errors import NotFoundError

            raise NotFoundError("结算案件不存在")
        return row

    def list_cases(self, *, status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute(
                "SELECT * FROM settlement_cases WHERE status=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM settlement_cases ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_case(self, case_id: int, *, status: str | None = None, current_version: int | None = None, now: str) -> None:
        self.connection.execute(
            "UPDATE settlement_cases SET status=COALESCE(?,status),current_version=?,updated_at=? WHERE id=?",
            (status, current_version, now, case_id),
        )

    # ---- 版本 ----

    def create_version(self, *, case_id: int, version: int, basis: str, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO settlement_versions(case_id,version,status,basis,created_by,created_at) VALUES(?,?,'draft',?,?,?)",
            (case_id, version, basis, created_by, now),
        )
        return dict(self.require_version(int(cursor.lastrowid)))

    def require_version(self, version_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM settlement_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            from app.core.errors import NotFoundError

            raise NotFoundError("结算版本不存在")
        return row

    def version_by_no(self, case_id: int, version: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM settlement_versions WHERE case_id=? AND version=?", (case_id, version)
        ).fetchone()

    def latest_version(self, case_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM settlement_versions WHERE case_id=? ORDER BY version DESC LIMIT 1", (case_id,)
        ).fetchone()

    def list_versions(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM settlement_versions WHERE case_id=? ORDER BY version", (case_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def update_version(self, version_id: int, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key}=?" for key in fields)
        self.connection.execute(
            f"UPDATE settlement_versions SET {columns} WHERE id=?",
            (*fields.values(), version_id),
        )

    # ---- 条目 ----

    def upsert_draft_entry(self, *, version_id: int, case_id: int, entry: dict[str, Any], actor: str, now: str) -> str:
        """草稿条目按 (entry_type,line_key) 幂等写入；返回 inserted/updated/unchanged。"""
        existing = self.connection.execute(
            "SELECT * FROM settlement_entries WHERE version_id=? AND entry_type=? AND line_key=?",
            (version_id, entry["entry_type"], entry["line_key"]),
        ).fetchone()
        params = {
            "version_id": version_id,
            "case_id": case_id,
            "entry_type": entry["entry_type"],
            "line_key": entry["line_key"],
            "category": entry["category"],
            "counterparty": entry["counterparty"],
            "description": entry["description"],
            "amount_cents": entry["amount_cents"],
            "quantity_cents": entry["quantity_cents"],
            "unit_price_cents": entry["unit_price_cents"],
            "source_ref": entry["source_ref"],
            "voucher_no": entry["voucher_no"],
            "voucher_required": 1 if entry["voucher_required"] else 0,
            "flags_json": json.dumps(entry.get("flags", []), ensure_ascii=False),
            "import_batch": entry["import_batch"],
            "actor": actor,
            "now": now,
        }
        if existing is None:
            sequence = int(self.connection.execute(
                "SELECT COALESCE(MAX(sequence_no),0)+1 FROM settlement_entries WHERE version_id=?", (version_id,)
            ).fetchone()[0])
            self.connection.execute(
                "INSERT INTO settlement_entries(version_id,case_id,sequence_no,entry_type,line_key,category,counterparty,"
                "description,amount_cents,quantity_cents,unit_price_cents,source_ref,voucher_no,voucher_required,flags_json,"
                "import_batch,created_by,updated_by,created_at,updated_at) VALUES("
                ":version_id,:case_id,:sequence_no,:entry_type,:line_key,:category,:counterparty,:description,"
                ":amount_cents,:quantity_cents,:unit_price_cents,:source_ref,:voucher_no,:voucher_required,:flags_json,"
                ":import_batch,:actor,:actor,:now,:now)",
                {**params, "sequence_no": sequence},
            )
            return "inserted"
        changed = int(existing["amount_cents"]) != params["amount_cents"] or any(
            (existing[key] if existing[key] is not None else "") != (params[key] if params[key] is not None else "")
            for key in ("category", "counterparty", "description", "source_ref", "voucher_no", "quantity_cents", "unit_price_cents", "voucher_required")
        )
        self.connection.execute(
            "UPDATE settlement_entries SET category=:category,counterparty=:counterparty,description=:description,"
            "amount_cents=:amount_cents,quantity_cents=:quantity_cents,unit_price_cents=:unit_price_cents,"
            "source_ref=:source_ref,voucher_no=:voucher_no,voucher_required=:voucher_required,flags_json=:flags_json,"
            "import_batch=:import_batch,updated_by=:actor,updated_at=:now WHERE id=:entry_id",
            {**params, "entry_id": existing["id"]},
        )
        return "updated" if changed else "unchanged"

    def entries_for_version(self, version_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM settlement_entries WHERE version_id=? ORDER BY sequence_no,id", (version_id,)
        ).fetchall()
        return [self._entry_dict(row) for row in rows]

    def clear_draft_entries(self, version_id: int) -> None:
        self.connection.execute("DELETE FROM settlement_entries WHERE version_id=?", (version_id,))

    # ---- 导入幂等记录 ----

    def get_import(self, case_id: int, import_batch: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM settlement_imports WHERE case_id=? AND import_batch=?", (case_id, import_batch)
        ).fetchone()

    def insert_import(self, *, case_id: int, version_id: int, import_batch: str, request_digest: str, entry_count: int, created_by: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO settlement_imports(case_id,version_id,import_batch,request_digest,entry_count,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (case_id, version_id, import_batch, request_digest, entry_count, created_by, now),
        )

    def list_imports(self, case_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM settlement_imports WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()]

    # ---- 审计 ----

    def add_audit(self, *, case_id: int | None, version_id: int | None, version_no: int | None, action: str, actor: str, before: dict[str, Any], after: dict[str, Any], metadata: dict[str, Any], now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO settlement_audit(case_id,version_id,version_no,action,actor,before_json,after_json,metadata_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (case_id, version_id, version_no, action, actor,
             json.dumps(before, ensure_ascii=False, sort_keys=True, default=str),
             json.dumps(after, ensure_ascii=False, sort_keys=True, default=str),
             json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str), now),
        )
        return int(cursor.lastrowid)

    def audit_for_case(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM settlement_audit WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def audit_for_version(self, version_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM settlement_audit WHERE version_id=? ORDER BY id", (version_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _entry_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["flags"] = json.loads(value.pop("flags_json") or "[]")
        return value
