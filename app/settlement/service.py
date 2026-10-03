from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.settlement.money import to_cents, yuan
from app.settlement.repository import SettlementRepository
from app.services.audit import AuditContext, AuditService


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


# 确认/发布时需要固化的条目字段，保证快照摘要不受行编号等易变列影响
SNAPSHOT_FIELDS = (
    "entry_type", "line_key", "category", "counterparty", "description",
    "amount_cents", "quantity_cents", "unit_price_cents", "source_ref",
    "voucher_no", "voucher_required",
)


class SettlementService:
    """结算案件的版本化晋级、不可变快照、幂等导入与审计链。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = SettlementRepository(self.connection)

    # ---- 案件 ----

    def create_case(self, payload: dict[str, Any], actor: str, actor_user_id: int | None = None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SettlementRepository(connection)
            if repository.case_by_code(payload["case_code"]):
                raise ConflictError("结算案件编码已存在")
            case = repository.create_case(
                case_code=payload["case_code"], ceremony_type=payload.get("ceremony_type", ""),
                family_contact=payload.get("family_contact", ""), created_by=actor, now=now,
            )
            version = repository.create_version(case_id=case["id"], version=1, basis="初次结算", created_by=actor, now=now)
            self._audit(connection, case["id"], version["id"], 1, "case.create", actor, actor_user_id,
                        after={"case_code": case["case_code"]}, metadata={"version_id": version["id"]}, now=now)
            return self._case_view(connection, case["id"])

    def get_case(self, case_id: int) -> dict[str, Any]:
        with transaction() as connection:
            return self._case_view(connection, case_id)

    def list_cases(self, *, status: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        rows = self.repository.list_cases(status=status, limit=max(1, min(limit, 500)), offset=max(0, offset))
        return {"items": [self._case_summary(row) for row in rows]}

    # ---- 版本与回放 ----

    def list_versions(self, case_id: int) -> dict[str, Any]:
        case = self.repository.require_case(case_id)
        versions = self.repository.list_versions(case_id)
        return {"case": self._case_summary(dict(case)), "versions": [self._version_summary(row) for row in versions]}

    def get_version(self, case_id: int, version_no: int) -> dict[str, Any]:
        case = self.repository.require_case(case_id)
        row = self.repository.version_by_no(case_id, version_no)
        if row is None:
            raise NotFoundError("结算版本不存在")
        version = dict(row)
        version["entries"] = [self._entry_dict(item) for item in self.repository.entries_for_version(version["id"])]
        version["diff"] = json.loads(version["diff_json"]) if version.get("diff_json") else None
        version["audit_chain"] = self.repository.audit_for_version(version["id"])
        return {"case": self._case_summary(dict(case)), "version": self._version_summary(version, nested=True)}

    def audit_chain(self, case_id: int) -> dict[str, Any]:
        case = self.repository.require_case(case_id)
        return {"case": self._case_summary(dict(case)), "events": self.repository.audit_for_case(case_id)}

    # ---- 草稿导入与计算 ----

    def import_entries(self, case_id: int, payload: dict[str, Any], actor: str, actor_user_id: int | None = None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        normalized = [self._normalize_entry(raw, index) for index, raw in enumerate(payload["entries"])]
        request_digest = digest({"entries": normalized, "replace": bool(payload.get("replace", False))})
        with transaction(immediate=True) as connection:
            repository = SettlementRepository(connection)
            case = repository.require_case(case_id)
            existing_import = repository.get_import(case_id, payload["import_batch"])
            if existing_import is not None:
                if existing_import["request_digest"] != request_digest:
                    raise ConflictError(
                        "同一导入批次对应了不同的费用数据",
                        context={"import_batch": payload["import_batch"],
                                 "original_version": existing_import["version_id"]},
                    )
                return self._replay_import(connection, case, existing_import)
            draft = self._require_latest_draft(repository, case_id)
            if payload.get("replace", False):
                repository.clear_draft_entries(draft["id"])
            results = {"inserted": 0, "updated": 0, "unchanged": 0}
            for entry in normalized:
                entry["import_batch"] = payload["import_batch"]
                outcome = repository.upsert_draft_entry(
                    version_id=draft["id"], case_id=case_id, entry=entry, actor=actor, now=now,
                )
                results[outcome] += 1
            repository.insert_import(
                case_id=case_id, version_id=draft["id"], import_batch=payload["import_batch"],
                request_digest=request_digest, entry_count=len(normalized), created_by=actor, now=now,
            )
            totals = self._recalculate(connection, draft["id"])
            self._audit(connection, case_id, draft["id"], draft["version"], "entries.import", actor, actor_user_id,
                        after={"import_batch": payload["import_batch"], "results": results, "totals": totals},
                        metadata={"replace": bool(payload.get("replace", False)), "note": payload.get("note", "")}, now=now)
            return {
                "replayed": False,
                "import_batch": payload["import_batch"],
                "version": draft["version"],
                "results": results,
                "totals": self._money_view(totals),
            }

    def recalculate(self, case_id: int, actor: str, actor_user_id: int | None = None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SettlementRepository(connection)
            draft = self._require_latest_draft(repository, case_id)
            totals = self._recalculate(connection, draft["id"])
            self._audit(connection, case_id, draft["id"], draft["version"], "entries.recalculate", actor,
                        actor_user_id, after={"totals": totals}, now=now)
            return {"version": draft["version"], "totals": self._money_view(totals)}

    # ---- 晋级：确认 → 发布 → （撤销/替代） ----

    def confirm(self, case_id: int, actor: str, actor_user_id: int | None = None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SettlementRepository(connection)
            case = repository.require_case(case_id)
            draft = repository.latest_version(case_id)
            if draft is None:
                raise NotFoundError("结算版本不存在")
            if draft["status"] == "published":
                raise ConflictError("当前版本已发布，不能重复确认")
            if draft["status"] == "revoked":
                raise ConflictError("当前版本已撤销，请基于新版本编制")
            if draft["status"] == "confirmed":
                return self._version_payload(connection, case, draft)
            entries = repository.entries_for_version(draft["id"])
            issues = self._validate(entries)
            if issues:
                raise ValidationError("结算条目未通过发布前校验", context={"issues": issues})
            totals = self._totals(entries)
            predecessor = self._published_version(repository, case_id)
            chain_base = predecessor
            if chain_base is None and draft["version"] > 1:
                candidate = repository.version_by_no(case_id, int(draft["version"]) - 1)
                if candidate is not None and candidate["snapshot_digest"]:
                    chain_base = candidate
            predecessor_digest = chain_base["snapshot_digest"] if chain_base is not None else ""
            snapshot_digest = digest({
                "case_code": case["case_code"], "version": draft["version"], "currency": "CNY",
                "basis": draft["basis"], "totals": totals,
                "entries": [{key: entry[key] for key in SNAPSHOT_FIELDS} for entry in entries],
                "predecessor_snapshot_digest": predecessor_digest,
            })
            input_digest = digest([{key: entry[key] for key in SNAPSHOT_FIELDS} for entry in entries])
            repository.update_version(
                draft["id"], status="confirmed", **{f"{name}_cents": totals[name] for name in totals},
                input_digest=input_digest, snapshot_digest=snapshot_digest,
                confirmed_by=actor, confirmed_at=now,
            )
            self._audit(connection, case_id, draft["id"], draft["version"], "version.confirm", actor,
                        actor_user_id, before={"status": "draft"}, after={"status": "confirmed", "totals": totals},
                        metadata={"snapshot_digest": snapshot_digest}, now=now)
            return self._version_payload(connection, case, repository.require_version(draft["id"]))

    def publish(self, case_id: int, actor: str, actor_user_id: int | None = None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SettlementRepository(connection)
            case = repository.require_case(case_id)
            target = repository.latest_version(case_id)
            if target is None:
                raise NotFoundError("结算版本不存在")
            if target["status"] == "published":
                return self._version_payload(connection, case, target)
            if target["status"] != "confirmed":
                raise ConflictError("只有已确认的结算版本才能发布")
            entries = repository.entries_for_version(target["id"])
            issues = self._validate(entries)
            if issues:
                raise ValidationError("结算条目未通过发布前校验", context={"issues": issues})
            predecessor = self._published_version(repository, case_id)
            prior_for_diff = predecessor
            if prior_for_diff is None and target["version"] > 1:
                candidate = repository.version_by_no(case_id, int(target["version"]) - 1)
                if candidate is not None and candidate["status"] in {"confirmed", "revoked"} and candidate["snapshot_digest"]:
                    prior_for_diff = candidate
            diff_payload: dict[str, Any] | None = None
            if prior_for_diff is not None:
                prior_entries = repository.entries_for_version(prior_for_diff["id"])
                diff_payload = self._diff(prior_entries, entries)
            if predecessor is not None:
                repository.update_version(
                    predecessor["id"], status="revoked", revoked_by=actor, revoked_at=now,
                    revoke_reason=f"被版本 v{target['version']} 替代",
                )
                self._audit(connection, case_id, predecessor["id"], predecessor["version"],
                            "version.supersede", actor, actor_user_id,
                            before={"status": "published"}, after={"status": "revoked"},
                            metadata={"superseded_by": target["version"], "diff": diff_payload}, now=now)
            repository.update_version(
                target["id"], status="published", published_by=actor, published_at=now,
                supersedes_version=predecessor["version"] if predecessor is not None else None,
                diff_json=json.dumps(diff_payload, ensure_ascii=False, sort_keys=True) if diff_payload is not None else None,
            )
            repository.update_case(case_id, status="published", current_version=target["version"], now=now)
            self._audit(connection, case_id, target["id"], target["version"], "version.publish", actor,
                        actor_user_id, before={"status": "confirmed"}, after={"status": "published"},
                        metadata={"supersedes_version": predecessor["version"] if predecessor is not None else None,
                                  "diff_base_version": prior_for_diff["version"] if prior_for_diff is not None and prior_for_diff is not predecessor else None,
                                  "diff": diff_payload}, now=now)
            return self._version_payload(connection, case, repository.require_version(target["id"]))

    def revoke(self, case_id: int, reason: str, actor: str, actor_user_id: int | None = None) -> dict[str, Any]:
        """复核人撤销最近的已发布版本（作废生效快照）或已确认未发布版本（拒绝发布）。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SettlementRepository(connection)
            case = repository.require_case(case_id)
            target = repository.latest_version(case_id)
            if target is None or target["status"] not in {"published", "confirmed"}:
                raise ConflictError("当前案件没有可撤销的已确认或已发布版本")
            from_status = target["status"]
            repository.update_version(
                target["id"], status="revoked", revoked_by=actor, revoked_at=now, revoke_reason=reason,
            )
            active = self._published_version(repository, case_id)
            if active is not None:
                # 撤销的是待发布的新版本，更早的已发布版本继续生效
                repository.update_case(case_id, status="published", current_version=active["version"], now=now)
            elif from_status == "published":
                repository.update_case(case_id, status="revoked", current_version=target["version"], now=now)
            else:
                repository.update_case(case_id, current_version=target["version"], now=now)
            self._audit(connection, case_id, target["id"], target["version"], "version.revoke", actor,
                        actor_user_id, before={"status": from_status}, after={"status": "revoked"},
                        metadata={"reason": reason, "still_effective_version": active["version"] if active is not None else None}, now=now)
            return self._version_payload(connection, case, repository.require_version(target["id"]))

    def new_version(self, case_id: int, payload: dict[str, Any], actor: str, actor_user_id: int | None = None) -> dict[str, Any]:
        """基于最近一个已发布/撤销版本创建新的草稿，并结转其条目作为修订基线。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SettlementRepository(connection)
            case = repository.require_case(case_id)
            latest = repository.latest_version(case_id)
            if latest is not None and latest["status"] == "draft":
                raise ConflictError("当前案件已有草稿版本，请先确认或发布后再修订")
            if latest is not None and latest["status"] == "confirmed":
                raise ConflictError("当前版本已确认待发布，请由复核人发布或撤销后再修订")
            base = latest
            if base is None:
                raise NotFoundError("结算版本不存在")
            next_no = int(base["version"]) + 1
            draft = repository.create_version(
                case_id=case_id, version=next_no,
                basis=payload.get("basis", "") or f"在版本 v{base['version']} 基础上补录修订",
                created_by=actor, now=now,
            )
            carried = 0
            for entry in repository.entries_for_version(base["id"]):
                carried += 1
                repository.upsert_draft_entry(
                    version_id=draft["id"], case_id=case_id,
                    entry={**{key: entry[key] for key in SNAPSHOT_FIELDS},
                           "flags": [*entry.get("flags", []), "carried_forward"],
                           "import_batch": f"carryforward:v{base['version']}"},
                    actor=actor, now=now,
                )
            totals = self._recalculate(connection, draft["id"])
            self._audit(connection, case_id, draft["id"], next_no, "version.create", actor, actor_user_id,
                        after={"status": "draft", "basis": draft["basis"], "carried_entries": carried, "totals": totals},
                        metadata={"based_on": base["version"], "note": payload.get("note", "")}, now=now)
            return self._version_payload(connection, case, repository.require_version(draft["id"]))

    # ---- 内部规则 ----

    @staticmethod
    def _require_latest_draft(repository: SettlementRepository, case_id: int) -> sqlite3.Row:
        draft = repository.latest_version(case_id)
        if draft is None:
            raise NotFoundError("结算版本不存在")
        if draft["status"] != "draft":
            raise ConflictError("只有草稿版本可以导入费用或重新计算；如需修订请创建新版本")
        return draft

    @staticmethod
    def _published_version(repository: SettlementRepository, case_id: int) -> sqlite3.Row | None:
        return repository.connection.execute(
            "SELECT * FROM settlement_versions WHERE case_id=? AND status='published' ORDER BY version DESC LIMIT 1",
            (case_id,),
        ).fetchone()

    def _recalculate(self, connection: sqlite3.Connection, version_id: int) -> dict[str, int]:
        repository = SettlementRepository(connection)
        entries = repository.entries_for_version(version_id)
        totals = self._totals(entries)
        input_digest = digest([{key: entry[key] for key in SNAPSHOT_FIELDS} for entry in entries])
        repository.update_version(version_id, **{f"{name}_cents": totals[name] for name in totals}, input_digest=input_digest)
        return totals

    @staticmethod
    def _totals(entries: list[dict[str, Any]]) -> dict[str, int]:
        payables = sum(int(item["amount_cents"]) for item in entries if item["entry_type"] == "payable")
        receipts = sum(int(item["amount_cents"]) for item in entries if item["entry_type"] == "receipt")
        return {"payables": payables, "receipts": receipts, "net": receipts - payables}

    def _validate(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        if not entries:
            issues.append({"code": "empty_version", "message": "版本没有任何费用条目，不能发布"})
        for entry in entries:
            pointer = {"entry_type": entry["entry_type"], "line_key": entry["line_key"],
                       "category": entry["category"], "source_ref": entry["source_ref"]}
            if int(entry["amount_cents"]) <= 0:
                issues.append({"code": "non_positive_amount", "message": "费用金额必须大于零", **pointer,
                               "amount": yuan(int(entry["amount_cents"]))})
            if entry["voucher_required"] and not entry["voucher_no"]:
                issues.append({"code": "missing_voucher", "message": "缺少凭证号", **pointer,
                               "amount": yuan(int(entry["amount_cents"]))})
            quantity = entry.get("quantity_cents")
            unit_price = entry.get("unit_price_cents")
            if quantity is not None and unit_price is not None:
                expected = int(quantity) * int(unit_price) // 100
                # 用量与单价均以分存储，乘积可能产生亚分误差，容忍 1 分
                if abs(expected - int(entry["amount_cents"])) > 1:
                    issues.append({"code": "amount_mismatch",
                                   "message": "金额与用量×单价不一致，存在金额冲突",
                                   **pointer, "amount": yuan(int(entry["amount_cents"])),
                                   "expected_amount": yuan(expected)})
        # 同一业务单据在同方向上出现不同金额 => 金额冲突
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entry in entries:
            if entry["source_ref"]:
                groups.setdefault((entry["entry_type"], entry["source_ref"]), []).append(entry)
        for (entry_type, source_ref), members in groups.items():
            amounts = {int(item["amount_cents"]) for item in members}
            if len(amounts) > 1:
                issues.append({
                    "code": "amount_conflict",
                    "message": f"单据 {source_ref} 在同方向上存在不一致金额",
                    "entry_type": entry_type,
                    "source_ref": source_ref,
                    "amounts": sorted(yuan(value) for value in amounts),
                    "line_keys": [item["line_key"] for item in members],
                })
        return issues

    def _diff(self, old_entries: list[dict[str, Any]], new_entries: list[dict[str, Any]]) -> dict[str, Any]:
        old_map = {(item["entry_type"], item["line_key"]): item for item in old_entries}
        new_map = {(item["entry_type"], item["line_key"]): item for item in new_entries}
        changes: list[dict[str, Any]] = []
        for key in sorted(new_map):
            current = new_map[key]
            if key not in old_map:
                changes.append({"change": "added", "entry_type": key[0], "line_key": key[1],
                                "category": current["category"],
                                "amount_cents": int(current["amount_cents"]),
                                "amount_delta_cents": int(current["amount_cents"]),
                                "amount": yuan(int(current["amount_cents"]))})
                continue
            previous = old_map[key]
            delta = int(current["amount_cents"]) - int(previous["amount_cents"])
            field_changes = {
                field: {"before": yuan(int(previous[field])) if field == "amount_cents" else previous[field],
                        "after": yuan(int(current[field])) if field == "amount_cents" else current[field]}
                for field in ("amount_cents", "category", "counterparty", "source_ref", "voucher_no")
                if previous[field] != current[field]
            }
            if field_changes:
                changes.append({"change": "changed", "entry_type": key[0], "line_key": key[1],
                                "category": current["category"], "fields": field_changes,
                                "amount_delta_cents": delta,
                                "amount_delta": yuan(delta)})
        for key in sorted(old_map.keys() - new_map.keys()):
            previous = old_map[key]
            changes.append({"change": "removed", "entry_type": key[0], "line_key": key[1],
                            "category": previous["category"],
                            "amount_cents": int(previous["amount_cents"]),
                            "amount_delta_cents": -int(previous["amount_cents"]),
                            "amount": yuan(int(previous["amount_cents"]))})
        old_totals, new_totals = self._totals(old_entries), self._totals(new_entries)
        totals = {name: {"before_cents": old_totals[name], "after_cents": new_totals[name],
                         "before": yuan(old_totals[name]), "after": yuan(new_totals[name]),
                         "delta_cents": new_totals[name] - old_totals[name],
                         "delta": yuan(new_totals[name] - old_totals[name])}
                  for name in ("payables", "receipts", "net")}
        return {
            "added": sum(1 for item in changes if item["change"] == "added"),
            "removed": sum(1 for item in changes if item["change"] == "removed"),
            "changed": sum(1 for item in changes if item["change"] == "changed"),
            "entries": changes,
            "totals": totals,
        }

    def _normalize_entry(self, raw: dict[str, Any], index: int) -> dict[str, Any]:
        amount_cents = to_cents(raw["amount"], field=f"entries[{index}].amount")
        quantity_cents = to_cents(raw["quantity"], field=f"entries[{index}].quantity") if raw.get("quantity") is not None else None
        unit_price_cents = to_cents(raw["unit_price"], field=f"entries[{index}].unit_price") if raw.get("unit_price") is not None else None
        line_key = (raw.get("line_key") or "").strip()
        if not line_key:
            basis = raw.get("source_ref") or digest({
                "entry_type": raw["entry_type"], "category": raw["category"],
                "counterparty": raw.get("counterparty", ""), "description": raw.get("description", ""),
                "amount_cents": amount_cents, "source_ref": raw.get("source_ref", ""),
            })[:16]
            line_key = f"{raw['entry_type']}:{basis}"
        return {
            "entry_type": raw["entry_type"],
            "line_key": line_key,
            "category": raw["category"].strip(),
            "counterparty": (raw.get("counterparty") or "").strip(),
            "description": (raw.get("description") or "").strip(),
            "amount_cents": amount_cents,
            "quantity_cents": quantity_cents,
            "unit_price_cents": unit_price_cents,
            "source_ref": (raw.get("source_ref") or "").strip(),
            "voucher_no": (raw.get("voucher_no") or "").strip(),
            "voucher_required": bool(raw.get("voucher_required", True)),
            "flags": [],
        }

    # ---- 视图与审计 ----

    def _replay_import(self, connection: sqlite3.Connection, case: sqlite3.Row, existing_import: sqlite3.Row) -> dict[str, Any]:
        repository = SettlementRepository(connection)
        version = repository.require_version(existing_import["version_id"])
        totals = {name: int(version[f"{name}_cents"]) for name in ("payables", "receipts", "net")}
        return {
            "replayed": True,
            "import_batch": existing_import["import_batch"],
            "version": version["version"],
            "results": {"inserted": 0, "updated": 0, "unchanged": int(existing_import["entry_count"])},
            "totals": self._money_view(totals),
        }

    def _audit(self, connection: sqlite3.Connection, case_id: int, version_id: int | None, version_no: int | None,
               action: str, actor: str, actor_user_id: int | None, *, before: dict[str, Any] | None = None,
               after: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None, now: str) -> None:
        repository = SettlementRepository(connection)
        repository.add_audit(
            case_id=case_id, version_id=version_id, version_no=version_no, action=action, actor=actor,
            before=before or {}, after=after or {}, metadata=metadata or {}, now=now,
        )
        AuditService(connection, self.clock).record(
            AuditContext(actor_user_id, actor),
            action=f"settlement.{action}", resource_type="settlement_case", resource_id=case_id,
            before=before, after=after, metadata={**(metadata or {}), "version_no": version_no},
        )

    def _case_view(self, connection: sqlite3.Connection, case_id: int) -> dict[str, Any]:
        repository = SettlementRepository(connection)
        case = dict(repository.require_case(case_id))
        versions = repository.list_versions(case_id)
        case["versions"] = [self._version_summary(row) for row in versions]
        case["imports"] = repository.list_imports(case_id)
        return case

    def _version_payload(self, connection: sqlite3.Connection, case: sqlite3.Row, version: sqlite3.Row) -> dict[str, Any]:
        repository = SettlementRepository(connection)
        view = dict(version)
        view["entries"] = [self._entry_dict(item) for item in repository.entries_for_version(version["id"])]
        view["diff"] = json.loads(view["diff_json"]) if view.get("diff_json") else None
        return {"case": self._case_summary(dict(case)), "version": self._version_summary(view, nested=True)}

    @staticmethod
    def _case_summary(case: dict[str, Any]) -> dict[str, Any]:
        return {key: case[key] for key in ("id", "case_code", "ceremony_type", "family_contact", "status", "current_version", "created_by", "created_at", "updated_at") if key in case}

    def _version_summary(self, version: dict[str, Any], *, nested: bool = False) -> dict[str, Any]:
        summary = {
            "id": version["id"], "version": version["version"], "status": version["status"],
            "currency": version["currency"], "basis": version["basis"],
            "payables_cents": version["payables_cents"], "receipts_cents": version["receipts_cents"], "net_cents": version["net_cents"],
            "payables": yuan(int(version["payables_cents"])),
            "receipts": yuan(int(version["receipts_cents"])),
            "net": yuan(int(version["net_cents"])),
            "input_digest": version["input_digest"], "snapshot_digest": version["snapshot_digest"],
            "supersedes_version": version["supersedes_version"],
            "created_by": version["created_by"], "created_at": version["created_at"],
            "confirmed_by": version["confirmed_by"], "confirmed_at": version["confirmed_at"],
            "published_by": version["published_by"], "published_at": version["published_at"],
            "revoked_by": version["revoked_by"], "revoked_at": version["revoked_at"], "revoke_reason": version["revoke_reason"],
        }
        if "diff" in version:
            summary["diff"] = version["diff"]
        if nested:
            summary["entries"] = version.get("entries", [])
            summary["audit_chain"] = version.get("audit_chain", [])
        return summary

    @staticmethod
    def _entry_dict(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "sequence_no": entry["sequence_no"], "entry_type": entry["entry_type"], "line_key": entry["line_key"],
            "category": entry["category"], "counterparty": entry["counterparty"], "description": entry["description"],
            "amount_cents": entry["amount_cents"], "amount": yuan(int(entry["amount_cents"])),
            "quantity_cents": entry["quantity_cents"],
            "quantity": yuan(entry["quantity_cents"]) if entry["quantity_cents"] is not None else None,
            "unit_price_cents": entry["unit_price_cents"],
            "unit_price": yuan(entry["unit_price_cents"]) if entry["unit_price_cents"] is not None else None,
            "source_ref": entry["source_ref"], "voucher_no": entry["voucher_no"],
            "voucher_required": bool(entry["voucher_required"]), "flags": entry.get("flags", []),
            "import_batch": entry["import_batch"], "created_by": entry["created_by"], "updated_by": entry["updated_by"],
            "created_at": entry["created_at"], "updated_at": entry["updated_at"],
        }

    @staticmethod
    def _money_view(totals: dict[str, int]) -> dict[str, Any]:
        return {name: {"cents": totals[name], "amount": yuan(totals[name])} for name in ("payables", "receipts", "net")}
