from __future__ import annotations

from app.database import close_connection, get_connection
from app.settlement.service import SettlementService


def _auth(client, headers, username: str, password: str = "Settle!2345") -> dict:
    login = client.post("/api/auth/login", json={"username": username, "password": password, "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _make_user(client, admin, username: str, role: str) -> dict:
    created = client.post(
        "/api/users",
        json={"username": username, "password": "Settle!2345", "display_name": username, "role_codes": [role]},
        headers=admin["headers"],
    )
    assert created.status_code == 201, created.text
    return _auth(client, admin, username)


def payable(line: str, amount, *, category="场地", source_ref="DOC-1", voucher="V-1", **extra) -> dict:
    payload = {
        "entry_type": "payable", "line_key": line, "category": category, "counterparty": "天堂礼仪公司",
        "description": line, "amount": amount, "source_ref": source_ref, "voucher_no": voucher,
    }
    payload.update(extra)
    return payload


def receipt(line: str, amount, *, source_ref="RCP-1", voucher="RC-1") -> dict:
    return {"entry_type": "receipt", "line_key": line, "category": "礼金", "counterparty": "家属",
            "description": line, "amount": amount, "source_ref": source_ref, "voucher_no": voucher}


def _create_case(client, headers, code="CASE-001") -> int:
    response = client.post(
        "/api/settlements/cases",
        json={"case_code": code, "ceremony_type": "葬礼", "family_contact": "李家属"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_draft_recalculate_and_idempotent_import(client, admin):
    clerk = _make_user(client, admin, "clerk1", "clerk")
    case_id = _create_case(client, clerk)
    batch = {
        "import_batch": "import-batch-001",
        "entries": [payable("venue-overtime", "1200.00"), receipt("gift-money", "3800.00")],
    }
    first = client.post(f"/api/settlements/cases/{case_id}/entries/import", json=batch, headers=clerk)
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False
    assert first.json()["results"]["inserted"] == 2
    assert first.json()["totals"]["payables"] == {"cents": 120000, "amount": "1200.00"}
    assert first.json()["totals"]["net"] == {"cents": 260000, "amount": "2600.00"}

    # 同一批次重复导入必须幂等：不新增条目
    second = client.post(f"/api/settlements/cases/{case_id}/entries/import", json=batch, headers=clerk)
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    detail = client.get(f"/api/settlements/cases/{case_id}/versions/1", headers=clerk).json()
    assert len(detail["version"]["entries"]) == 2

    # 相同批次键但数据不同必须冲突
    conflict = dict(batch, entries=[payable("venue-overtime", "1500.00"), receipt("gift-money", "3800.00")])
    rejected = client.post(f"/api/settlements/cases/{case_id}/entries/import", json=conflict, headers=clerk)
    assert rejected.status_code == 409

    # 草稿可以反复计算
    recalc = client.post(f"/api/settlements/cases/{case_id}/recalculate", headers=clerk)
    assert recalc.status_code == 200
    assert recalc.json()["totals"]["payables"]["cents"] == 120000


def test_missing_voucher_blocks_publish_with_itemized_issues(client, admin):
    reviewer = _make_user(client, admin, "reviewer1", "finance_reviewer")
    case_id = _create_case(client, reviewer, code="CASE-002")
    batch = {
        "import_batch": "import-batch-002",
        "entries": [
            payable("venue-overtime", "1200.00"),
            payable("supplier-usage", "660.00", source_ref="DOC-2", voucher=""),
        ],
    }
    imported = client.post(f"/api/settlements/cases/{case_id}/entries/import", json=batch, headers=reviewer)
    assert imported.status_code == 200

    # 确认阶段即被闸口拦截
    confirmed = client.post(f"/api/settlements/cases/{case_id}/confirm", headers=reviewer)
    assert confirmed.status_code == 422
    issues = confirmed.json()["error"]["context"]["issues"]
    missing = [item for item in issues if item["code"] == "missing_voucher"]
    assert len(missing) == 1
    assert missing[0]["line_key"] == "supplier-usage"
    assert missing[0]["source_ref"] == "DOC-2"

    # 未确认前发布同样被拦截
    published = client.post(f"/api/settlements/cases/{case_id}/publish", headers=reviewer)
    assert published.status_code == 409
    # 版本仍是草稿：重启语义 —— 状态只由显式晋级改变
    versions = client.get(f"/api/settlements/cases/{case_id}/versions", headers=reviewer).json()
    assert versions["versions"][0]["status"] == "draft"


def test_amount_conflict_blocks_publish(client, admin):
    reviewer = _make_user(client, admin, "reviewer2", "finance_reviewer")
    case_id = _create_case(client, reviewer, code="CASE-003")
    batch = {
        "import_batch": "import-batch-003",
        "entries": [
            payable("line-a", "1000.00", source_ref="DUP-DOC", voucher="V-1"),
            payable("line-b", "1200.00", source_ref="DUP-DOC", voucher="V-2"),
        ],
    }
    client.post(f"/api/settlements/cases/{case_id}/entries/import", json=batch, headers=reviewer)
    confirmed = client.post(f"/api/settlements/cases/{case_id}/confirm", headers=reviewer)
    assert confirmed.status_code == 422
    issues = confirmed.json()["error"]["context"]["issues"]
    conflicts = [item for item in issues if item["code"] == "amount_conflict"]
    assert len(conflicts) == 1
    assert conflicts[0]["source_ref"] == "DUP-DOC"
    assert set(conflicts[0]["line_keys"]) == {"line-a", "line-b"}
    assert conflicts[0]["amounts"] == ["1000.00", "1200.00"]


def test_only_reviewer_can_publish_or_revoke(client, admin):
    clerk = _make_user(client, admin, "clerk2", "clerk")
    reviewer = _make_user(client, admin, "reviewer3", "finance_reviewer")
    case_id = _create_case(client, clerk, code="CASE-004")
    batch = {"import_batch": "import-batch-004", "entries": [payable("venue", "800.00")]}
    client.post(f"/api/settlements/cases/{case_id}/entries/import", json=batch, headers=clerk)

    # 经办员可以确认但不能发布
    assert client.post(f"/api/settlements/cases/{case_id}/confirm", headers=clerk).status_code == 200
    denied = client.post(f"/api/settlements/cases/{case_id}/publish", headers=clerk)
    assert denied.status_code == 403

    # 复核人发布
    published = client.post(f"/api/settlements/cases/{case_id}/publish", headers=reviewer)
    assert published.status_code == 200, published.text
    assert published.json()["version"]["status"] == "published"
    assert published.json()["version"]["published_by"] == "reviewer3"

    # 经办员不能撤销
    revoked = client.post(
        f"/api/settlements/cases/{case_id}/revoke", json={"reason": "测试无权撤销"}, headers=clerk,
    )
    assert revoked.status_code == 403

    # 未认证访问被拒绝
    assert client.get(f"/api/settlements/cases/{case_id}").status_code == 401


def test_published_snapshot_immutable_and_supersede_keeps_diff(client, admin):
    reviewer = _make_user(client, admin, "reviewer4", "finance_reviewer")
    case_id = _create_case(client, reviewer, code="CASE-005")
    batch = {
        "import_batch": "import-batch-005",
        "entries": [payable("venue", "1000.00", source_ref="DOC-1", voucher="V-1"),
                    receipt("gift", "3000.00")],
    }
    client.post(f"/api/settlements/cases/{case_id}/entries/import", json=batch, headers=reviewer)
    client.post(f"/api/settlements/cases/{case_id}/confirm", headers=reviewer)
    v1 = client.post(f"/api/settlements/cases/{case_id}/publish", headers=reviewer).json()
    v1_digest = v1["version"]["snapshot_digest"]
    assert v1_digest

    # 已发布版本不可再导入/覆盖
    blocked = client.post(
        f"/api/settlements/cases/{case_id}/entries/import",
        json={"import_batch": "import-batch-006", "entries": [payable("venue", "9999.00", source_ref="DOC-9", voucher="V-9")]},
        headers=reviewer,
    )
    assert blocked.status_code == 409

    # 重复发布是幂等的
    again = client.post(f"/api/settlements/cases/{case_id}/publish", headers=reviewer)
    assert again.status_code == 200 and again.json()["version"]["version"] == 1

    # 家属补录：创建新版本（结转原条目）
    created = client.post(
        f"/api/settlements/cases/{case_id}/new-version",
        json={"basis": "仪式后补录礼金与场地加时", "note": "家属追加"}, headers=reviewer,
    )
    assert created.status_code == 201
    assert created.json()["version"]["version"] == 2
    v2_draft = client.get(f"/api/settlements/cases/{case_id}/versions/2", headers=reviewer).json()
    assert len(v2_draft["version"]["entries"]) == 2  # 结转

    client.post(
        f"/api/settlements/cases/{case_id}/entries/import",
        json={"import_batch": "import-batch-007", "entries": [
            payable("venue", "1300.00", source_ref="DOC-1", voucher="V-1"),      # 场地加时，金额变更
            payable("supplier", "450.00", source_ref="DOC-3", voucher="V-3"),   # 供应商实际用量，新增
        ]},
        headers=reviewer,
    )
    client.post(f"/api/settlements/cases/{case_id}/confirm", headers=reviewer)
    v2 = client.post(f"/api/settlements/cases/{case_id}/publish", headers=reviewer).json()

    # 旧版本被替代且保留快照与责任人
    old = client.get(f"/api/settlements/cases/{case_id}/versions/1", headers=reviewer).json()["version"]
    assert old["status"] == "revoked"
    assert old["snapshot_digest"] == v1_digest
    assert old["revoked_by"] == "reviewer4"
    assert "被版本 v2 替代" in old["revoke_reason"]

    # 新版本保留差异
    diff = v2["version"]["diff"]
    assert diff["added"] == 1 and diff["changed"] == 1
    changed = [item for item in diff["entries"] if item["change"] == "changed"][0]
    assert changed["line_key"] == "venue"
    assert changed["amount_delta_cents"] == 30000
    assert diff["totals"]["payables"]["before"] == "1000.00"
    assert diff["totals"]["payables"]["after"] == "1750.00"
    assert v2["version"]["supersedes_version"] == 1
    assert v2["version"]["snapshot_digest"] != v1_digest

    case = client.get(f"/api/settlements/cases/{case_id}", headers=reviewer).json()
    assert case["status"] == "published" and case["current_version"] == 2


def test_reviewer_revoke_requires_permission_and_keeps_chain(client, admin):
    reviewer = _make_user(client, admin, "reviewer5", "finance_reviewer")
    case_id = _create_case(client, reviewer, code="CASE-006")
    client.post(
        f"/api/settlements/cases/{case_id}/entries/import",
        json={"import_batch": "import-batch-008", "entries": [payable("venue", "500.00")]},
        headers=reviewer,
    )
    client.post(f"/api/settlements/cases/{case_id}/confirm", headers=reviewer)
    client.post(f"/api/settlements/cases/{case_id}/publish", headers=reviewer)
    revoked = client.post(
        f"/api/settlements/cases/{case_id}/revoke", json={"reason": "凭证事后核验无效"}, headers=reviewer,
    )
    assert revoked.status_code == 200
    assert revoked.json()["version"]["status"] == "revoked"

    # 撤销后修订：新版本草稿，发布时仍能与 v1 形成差异
    client.post(f"/api/settlements/cases/{case_id}/new-version", json={"basis": "重新整理凭证"}, headers=reviewer)
    client.post(
        f"/api/settlements/cases/{case_id}/entries/import",
        json={"import_batch": "import-batch-009", "entries": [payable("venue", "500.00", voucher="V-1B")]},
        headers=reviewer,
    )
    client.post(f"/api/settlements/cases/{case_id}/confirm", headers=reviewer)
    republished = client.post(f"/api/settlements/cases/{case_id}/publish", headers=reviewer).json()
    assert republished["version"]["version"] == 2 and republished["version"]["status"] == "published"
    assert republished["version"]["diff"]["changed"] == 1


def test_replay_detail_and_audit_chain_after_reopen(client, admin):
    reviewer = _make_user(client, admin, "reviewer6", "finance_reviewer")
    case_id = _create_case(client, reviewer, code="CASE-007")
    client.post(
        f"/api/settlements/cases/{case_id}/entries/import",
        json={"import_batch": "import-batch-010", "entries": [
            payable("venue", "200.00"), payable("supplier", "300.00", source_ref="DOC-2", voucher="V-2"),
        ]},
        headers=reviewer,
    )
    client.post(f"/api/settlements/cases/{case_id}/confirm", headers=reviewer)
    client.post(f"/api/settlements/cases/{case_id}/publish", headers=reviewer)

    # 模拟服务重启：释放线程连接后用全新连接访问，已发布仍是已发布，草稿不会误晋级
    close_connection()
    connection = get_connection()
    service = SettlementService(connection)
    case = service.get_case(case_id)
    assert case["status"] == "published"
    replayed = service.get_version(case_id, 1)
    assert replayed["version"]["status"] == "published"
    assert {entry["line_key"] for entry in replayed["version"]["entries"]} == {"venue", "supplier"}
    assert replayed["version"]["snapshot_digest"]
    chain = service.audit_chain(case_id)["events"]
    actions = [event["action"] for event in chain]
    assert actions == ["case.create", "entries.import", "version.confirm", "version.publish"]
    # 审计链记录责任人
    assert all(event["actor"] == "reviewer6" for event in chain)


def test_unfinished_settlement_survives_restart_as_draft(client, admin):
    clerk = _make_user(client, admin, "clerk3", "clerk")
    case_id = _create_case(client, clerk, code="CASE-008")
    client.post(
        f"/api/settlements/cases/{case_id}/entries/import",
        json={"import_batch": "import-batch-011", "entries": [payable("venue", "200.00")]},
        headers=clerk,
    )
    close_connection()
    service = SettlementService(get_connection())
    versions = service.list_versions(case_id)["versions"]
    assert versions[0]["status"] == "draft"


def test_confirmed_version_superseded_without_publish(client, admin):
    reviewer = _make_user(client, admin, "reviewer7", "finance_reviewer")
    case_id = _create_case(client, reviewer, code="CASE-009")
    client.post(
        f"/api/settlements/cases/{case_id}/entries/import",
        json={"import_batch": "import-batch-012", "entries": [payable("venue", "700.00")]},
        headers=reviewer,
    )
    client.post(f"/api/settlements/cases/{case_id}/confirm", headers=reviewer)
    # 已确认但未发布不能再开新版本；先撤销确认版本，再修订
    revoked = client.post(
        f"/api/settlements/cases/{case_id}/revoke", json={"reason": "确认后发现凭证需要补充"}, headers=reviewer,
    )
    assert revoked.status_code == 200
    assert revoked.json()["version"]["status"] == "revoked"
    client.post(f"/api/settlements/cases/{case_id}/new-version", json={"basis": "补凭证后重新确认"}, headers=reviewer)
    client.post(
        f"/api/settlements/cases/{case_id}/entries/import",
        json={"import_batch": "import-batch-013", "entries": [payable("venue", "700.00")]},
        headers=reviewer,
    )
    client.post(f"/api/settlements/cases/{case_id}/confirm", headers=reviewer)
    republished = client.post(f"/api/settlements/cases/{case_id}/publish", headers=reviewer)
    assert republished.status_code == 200
    assert republished.json()["version"]["status"] == "published"


def test_revoke_pending_v2_keeps_published_v1_effective(client, admin):
    reviewer = _make_user(client, admin, "reviewer8", "finance_reviewer")
    case_id = _create_case(client, reviewer, code="CASE-010")
    client.post(
        f"/api/settlements/cases/{case_id}/entries/import",
        json={"import_batch": "import-batch-014", "entries": [payable("venue", "700.00")]},
        headers=reviewer,
    )
    client.post(f"/api/settlements/cases/{case_id}/confirm", headers=reviewer)
    client.post(f"/api/settlements/cases/{case_id}/publish", headers=reviewer)
    client.post(f"/api/settlements/cases/{case_id}/new-version", json={"basis": "补录"}, headers=reviewer)
    client.post(
        f"/api/settlements/cases/{case_id}/entries/import",
        json={"import_batch": "import-batch-015", "entries": [payable("supplier", "120.00", source_ref="D2", voucher="V2")]},
        headers=reviewer,
    )
    client.post(f"/api/settlements/cases/{case_id}/confirm", headers=reviewer)
    refused = client.post(
        f"/api/settlements/cases/{case_id}/revoke", json={"reason": "供应商用量存疑，拒绝发布"}, headers=reviewer,
    )
    assert refused.status_code == 200
    case = client.get(f"/api/settlements/cases/{case_id}", headers=reviewer).json()
    assert case["status"] == "published" and case["current_version"] == 1
    v2 = client.get(f"/api/settlements/cases/{case_id}/versions/2", headers=reviewer).json()["version"]
    assert v2["status"] == "revoked" and v2["revoked_by"] == "reviewer8"
    v1 = client.get(f"/api/settlements/cases/{case_id}/versions/1", headers=reviewer).json()["version"]
    assert v1["status"] == "published"
