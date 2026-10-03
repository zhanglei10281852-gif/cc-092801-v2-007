from __future__ import annotations

GIFT = {
    "external_ref": "GIFT-001",
    "direction": "receivable",
    "category": "gift_money",
    "description": "亲友礼金补录",
    "amount": "5000.00",
}
OVERTIME = {
    "external_ref": "VENUE-OT-001",
    "direction": "payable",
    "category": "venue_overtime",
    "description": "告别厅加时2小时",
    "quantity": "2",
    "unit": "小时",
    "unit_price": "600.00",
    "amount": "1200.00",
    "voucher_no": "VH-1001",
}
SUPPLIER = {
    "external_ref": "SUP-001",
    "direction": "payable",
    "category": "supplier_usage",
    "description": "鲜花实际用量30束",
    "quantity": "30",
    "unit": "束",
    "unit_price": "45.00",
    "amount": "1350.00",
    "voucher_no": "VH-2001",
}


def make_user(client, admin, username: str, permissions: list[str]) -> dict:
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": f"role.{username}", "name": f"角色{username}", "permission_codes": permissions},
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Clerk!23456", "display_name": username, "role_codes": [f"role.{username}"]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Clerk!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


def create_settlement(client, headers, order_code: str = "ORDER-2026-0001") -> dict:
    response = client.post(
        "/api/settlements",
        headers=headers,
        json={"order_code": order_code, "family_name": "张家", "ceremony_type": "funeral", "ceremony_date": "2026-09-28"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def import_batch(client, headers, settlement_id: int, batch_key: str, items: list[dict]):
    return client.post(f"/api/settlements/{settlement_id}/imports", headers=headers, json={"batch_key": batch_key, "items": items})


def confirm(client, headers, settlement_id: int):
    response = client.post(f"/api/settlements/{settlement_id}/confirm", headers=headers, json={})
    assert response.status_code == 200, response.text
    return response.json()


def publish(client, headers, settlement_id: int):
    return client.post(f"/api/settlements/{settlement_id}/publish", headers=headers, json={})


def test_full_versioned_settlement_flow(client, admin):
    settlement = create_settlement(client, admin["headers"])
    settlement_id = settlement["id"]
    assert settlement["status"] == "draft"
    assert settlement["latest_version"] == 1
    assert settlement["published_version"] is None

    imported = import_batch(client, admin["headers"], settlement_id, "batch-001", [GIFT, OVERTIME, SUPPLIER])
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["replayed"] is False
    assert len(body["inserted"]) == 3
    assert body["payable_total_cents"] == 255000
    assert body["receivable_total_cents"] == 500000

    recalculated = client.post(f"/api/settlements/{settlement_id}/recalculate", headers=admin["headers"], json={"note": "月底复核"})
    assert recalculated.status_code == 200, recalculated.text
    assert recalculated.json()["calculation_count"] == 1
    assert recalculated.json()["payable_total_cents"] == 255000

    confirmed = confirm(client, admin["headers"], settlement_id)
    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_by"] == "admin"
    assert confirmed["snapshot"]["totals"] == {"payable_cents": 255000, "receivable_cents": 500000, "net_cents": 245000}
    assert confirmed["snapshot"]["payables"]["categories"][0]["category"] == "supplier_usage"
    assert confirmed["snapshot_digest"]

    published = publish(client, admin["headers"], settlement_id)
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert published.json()["published_by"] == "admin"
    assert published.json()["diff_from_previous"] is None

    detail = client.get(f"/api/settlements/{settlement_id}", headers=admin["headers"]).json()
    assert detail["status"] == "published"
    assert detail["published_version"] == 1
    assert [version["status"] for version in detail["versions"]] == ["published"]

    replay = client.get(f"/api/settlements/{settlement_id}/versions/1", headers=admin["headers"]).json()
    assert replay["snapshot"]["payables"]["total_cents"] == 255000
    assert replay["snapshot"]["receivables"]["total_cents"] == 500000
    assert len(replay["items"]) == 3
    assert len(replay["imports"]) == 1

    audit = client.get(f"/api/settlements/{settlement_id}/audit", headers=admin["headers"]).json()["items"]
    assert [event["action"] for event in audit] == [
        "settlement.create",
        "batch.import",
        "version.recalculate",
        "version.confirm",
        "version.publish",
    ]
    assert all(event["actor"] == "admin" for event in audit)


def test_import_idempotency_replay_and_item_dedup(client, admin):
    settlement = create_settlement(client, admin["headers"])
    settlement_id = settlement["id"]

    first = import_batch(client, admin["headers"], settlement_id, "batch-dup", [GIFT, OVERTIME]).json()
    second = import_batch(client, admin["headers"], settlement_id, "batch-dup", [GIFT, OVERTIME]).json()
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["inserted"] == first["inserted"]
    assert second["draft_version"] == 1

    changed = import_batch(client, admin["headers"], settlement_id, "batch-dup", [GIFT])
    assert changed.status_code == 409

    third = import_batch(client, admin["headers"], settlement_id, "batch-dup-2", [GIFT, SUPPLIER]).json()
    assert [entry["external_ref"] for entry in third["duplicates"]] == ["GIFT-001"]
    assert [entry["external_ref"] for entry in third["inserted"]] == ["SUP-001"]

    replay = client.get(f"/api/settlements/{settlement_id}/versions/1", headers=admin["headers"]).json()
    active = [item for item in replay["items"] if item["status"] == "active"]
    assert len(active) == 3
    assert replay["receivable_total_cents"] == 500000


def test_amount_conflict_blocks_publish_until_resolved(client, admin):
    settlement = create_settlement(client, admin["headers"])
    settlement_id = settlement["id"]
    import_batch(client, admin["headers"], settlement_id, "batch-1", [OVERTIME])

    conflicting = {**OVERTIME, "amount": "1500.00"}
    result = import_batch(client, admin["headers"], settlement_id, "batch-2", [conflicting]).json()
    assert result["conflicts"] == [
        {"item_id": result["conflicts"][0]["item_id"], "external_ref": "VENUE-OT-001", "existing_amount_cents": 120000, "incoming_amount_cents": 150000}
    ]

    confirm(client, admin["headers"], settlement_id)
    blocked = publish(client, admin["headers"], settlement_id)
    assert blocked.status_code == 409
    issues = blocked.json()["error"]["context"]["issues"]
    assert len(issues) == 1
    assert issues[0]["issue"] == "amount_conflict"
    assert issues[0]["external_ref"] == "VENUE-OT-001"
    assert issues[0]["existing_amount_cents"] == 120000
    assert issues[0]["incoming_amount_cents"] == 150000

    # 已确认版本不可再改，需要开启新版本处理冲突
    created = client.post(f"/api/settlements/{settlement_id}/versions", headers=admin["headers"], json={"note": "处理金额冲突"})
    assert created.status_code == 201, created.text
    carried = created.json()["items"][0]
    assert carried["conflict_flag"] is True

    resolved = client.post(
        f"/api/settlements/{settlement_id}/items/{carried['id']}/resolve",
        headers=admin["headers"],
        json={"action": "accept_incoming", "reason": "以供应商结算单为准"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["amount_cents"] == 150000
    assert resolved.json()["conflict_flag"] is False

    confirm(client, admin["headers"], settlement_id)
    published = publish(client, admin["headers"], settlement_id)
    assert published.status_code == 200, published.text
    assert published.json()["snapshot"]["payables"]["total_cents"] == 150000


def test_conflict_keep_existing_and_void_item(client, admin):
    settlement = create_settlement(client, admin["headers"])
    settlement_id = settlement["id"]
    import_batch(client, admin["headers"], settlement_id, "batch-1", [GIFT, OVERTIME])
    conflicting = {**OVERTIME, "amount": "1500.00"}
    result = import_batch(client, admin["headers"], settlement_id, "batch-2", [conflicting]).json()
    item_id = result["conflicts"][0]["item_id"]

    kept = client.post(
        f"/api/settlements/{settlement_id}/items/{item_id}/resolve",
        headers=admin["headers"],
        json={"action": "keep_existing", "reason": "重复导入的误单"},
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["amount_cents"] == 120000
    assert kept.json()["conflict_flag"] is False

    gift_id = [item for item in client.get(f"/api/settlements/{settlement_id}/versions/1", headers=admin["headers"]).json()["items"] if item["category"] == "gift_money"][0]["id"]
    voided = client.post(
        f"/api/settlements/{settlement_id}/items/{gift_id}/void",
        headers=admin["headers"],
        json={"reason": "家属撤回了这笔礼金"},
    )
    assert voided.status_code == 200, voided.text
    assert voided.json()["status"] == "voided"

    confirmed = confirm(client, admin["headers"], settlement_id)
    assert confirmed["receivable_total_cents"] == 0
    assert confirmed["payable_total_cents"] == 120000
    audit = client.get(f"/api/settlements/{settlement_id}/audit", headers=admin["headers"]).json()["items"]
    assert "item.resolve_conflict" in [event["action"] for event in audit]
    assert "item.void" in [event["action"] for event in audit]


def test_missing_voucher_blocks_publish_until_backfilled(client, admin):
    settlement = create_settlement(client, admin["headers"])
    settlement_id = settlement["id"]
    no_voucher = {key: value for key, value in OVERTIME.items() if key != "voucher_no"}
    import_batch(client, admin["headers"], settlement_id, "batch-1", [no_voucher, GIFT])

    confirm(client, admin["headers"], settlement_id)
    blocked = publish(client, admin["headers"], settlement_id)
    assert blocked.status_code == 409
    issues = blocked.json()["error"]["context"]["issues"]
    assert [issue["issue"] for issue in issues] == ["missing_voucher"]
    assert issues[0]["external_ref"] == "VENUE-OT-001"

    created = client.post(f"/api/settlements/{settlement_id}/versions", headers=admin["headers"], json={})
    assert created.status_code == 201
    backfill = import_batch(client, admin["headers"], settlement_id, "batch-2", [OVERTIME]).json()
    assert [entry["external_ref"] for entry in backfill["updated"]] == ["VENUE-OT-001"]

    confirm(client, admin["headers"], settlement_id)
    published = publish(client, admin["headers"], settlement_id)
    assert published.status_code == 200, published.text


def test_direction_category_mismatch_rejected(client, admin):
    settlement = create_settlement(client, admin["headers"])
    bad = {"external_ref": "X-1", "direction": "payable", "category": "gift_money", "amount": "1.00"}
    response = import_batch(client, admin["headers"], settlement["id"], "batch-1", [bad])
    assert response.status_code == 422


def test_publish_and_revoke_require_review_permission(client, admin):
    writer = make_user(client, admin, "settlement.writer", ["settlements.read", "settlements.write"])
    reviewer = make_user(client, admin, "settlement.reviewer", ["settlements.read", "settlements.review"])

    settlement = create_settlement(client, writer)
    settlement_id = settlement["id"]
    import_batch(client, writer, settlement_id, "batch-1", [GIFT])
    confirm(client, writer, settlement_id)

    assert publish(client, writer, settlement_id).status_code == 403
    assert client.post(f"/api/settlements/{settlement_id}/publish", json={}).status_code == 401
    # 只有复核权限的账号不能导入费用
    assert import_batch(client, reviewer, settlement_id, "batch-2", [GIFT]).status_code == 403

    published = publish(client, reviewer, settlement_id)
    assert published.status_code == 200, published.text
    assert published.json()["published_by"] == "settlement.reviewer"

    assert client.post(f"/api/settlements/{settlement_id}/revoke", headers=writer, json={"reason": "误发布"}).status_code == 403
    revoked = client.post(f"/api/settlements/{settlement_id}/revoke", headers=reviewer, json={"reason": "家属提出异议，重新核对"})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["revoked_by"] == "settlement.reviewer"
    assert revoked.json()["revoke_reason"] == "家属提出异议，重新核对"

    detail = client.get(f"/api/settlements/{settlement_id}", headers=admin["headers"]).json()
    assert detail["published_version"] is None
    assert detail["status"] == "draft"
    assert client.post(f"/api/settlements/{settlement_id}/revoke", headers=reviewer, json={"reason": "再次撤销"}).status_code == 409


def test_superseded_version_retains_diff_and_responsible(client, admin):
    reviewer = make_user(client, admin, "finance.reviewer", ["settlements.read", "settlements.review"])
    settlement = create_settlement(client, admin["headers"])
    settlement_id = settlement["id"]
    import_batch(client, admin["headers"], settlement_id, "batch-1", [GIFT, OVERTIME])
    confirm(client, admin["headers"], settlement_id)
    assert publish(client, reviewer, settlement_id).status_code == 200

    created = client.post(f"/api/settlements/{settlement_id}/versions", headers=admin["headers"], json={"note": "家属补录迟到礼金"})
    assert created.status_code == 201, created.text
    assert created.json()["version"] == 2
    assert len(created.json()["items"]) == 2

    extra_gift = {"external_ref": "GIFT-002", "direction": "receivable", "category": "gift_money", "description": "迟到礼金", "amount": "800.00"}
    import_batch(client, admin["headers"], settlement_id, "batch-2", [extra_gift])
    confirm(client, admin["headers"], settlement_id)
    published = publish(client, reviewer, settlement_id)
    assert published.status_code == 200, published.text

    diff = published.json()["diff_from_previous"]
    assert diff["previous_version"] == 1
    assert diff["current_version"] == 2
    assert diff["receivable_delta_cents"] == 80000
    assert diff["payable_delta_cents"] == 0
    assert diff["net_delta_cents"] == 80000
    assert diff["responsible"]["previous_published_by"] == "finance.reviewer"
    assert diff["responsible"]["published_by"] == "finance.reviewer"
    assert diff["responsible"]["confirmed_by"] == "admin"
    gift_delta = [entry for entry in diff["category_deltas"] if entry["category"] == "gift_money"][0]
    assert gift_delta["previous_cents"] == 500000
    assert gift_delta["current_cents"] == 580000

    first = client.get(f"/api/settlements/{settlement_id}/versions/1", headers=admin["headers"]).json()
    assert first["status"] == "superseded"
    assert first["superseded_by_version"] == 2
    assert first["snapshot"]["totals"]["receivable_cents"] == 500000

    detail = client.get(f"/api/settlements/{settlement_id}", headers=admin["headers"]).json()
    assert detail["published_version"] == 2
    assert [version["status"] for version in detail["versions"]] == ["superseded", "published"]

    audit = client.get(f"/api/settlements/{settlement_id}/audit", headers=admin["headers"]).json()["items"]
    supersede = [event for event in audit if event["action"] == "version.supersede"][0]
    assert supersede["detail"]["diff"]["receivable_delta_cents"] == 80000
    assert supersede["actor"] == "finance.reviewer"


def test_confirmed_snapshot_is_immutable(client, admin):
    settlement = create_settlement(client, admin["headers"])
    settlement_id = settlement["id"]
    import_batch(client, admin["headers"], settlement_id, "batch-1", [GIFT, OVERTIME])
    confirmed = confirm(client, admin["headers"], settlement_id)
    digest_v1 = confirmed["snapshot_digest"]

    # 已确认版本不能再导入或重算
    assert import_batch(client, admin["headers"], settlement_id, "batch-2", [SUPPLIER]).status_code == 409
    assert client.post(f"/api/settlements/{settlement_id}/recalculate", headers=admin["headers"], json={}).status_code == 409

    # 新版本中修改金额，旧版本快照保持原值
    assert client.post(f"/api/settlements/{settlement_id}/versions", headers=admin["headers"], json={}).status_code == 201
    changed = {**OVERTIME, "amount": "1800.00"}
    result = import_batch(client, admin["headers"], settlement_id, "batch-3", [changed]).json()
    assert result["conflicts"][0]["existing_amount_cents"] == 120000
    item_id = result["conflicts"][0]["item_id"]
    client.post(
        f"/api/settlements/{settlement_id}/items/{item_id}/resolve",
        headers=admin["headers"],
        json={"action": "accept_incoming", "reason": "供应商最终结算单"},
    )
    confirm(client, admin["headers"], settlement_id)

    first = client.get(f"/api/settlements/{settlement_id}/versions/1", headers=admin["headers"]).json()
    assert first["snapshot_digest"] == digest_v1
    assert first["snapshot"]["payables"]["total_cents"] == 120000
    second = client.get(f"/api/settlements/{settlement_id}/versions/2", headers=admin["headers"]).json()
    assert second["snapshot"]["payables"]["total_cents"] == 180000


def test_restart_does_not_promote_unfinished_settlements(client, admin):
    confirmed_settlement = create_settlement(client, admin["headers"], "ORDER-RESTART-1")
    import_batch(client, admin["headers"], confirmed_settlement["id"], "batch-1", [GIFT, OVERTIME])
    confirm(client, admin["headers"], confirmed_settlement["id"])
    draft_settlement = create_settlement(client, admin["headers"], "ORDER-RESTART-2")

    # 模拟服务重启：关闭连接并丢弃所有内存状态
    from app.database import close_connection

    close_connection()

    from app.settlement.service import SettlementService

    service = SettlementService()
    first = service.get_settlement(confirmed_settlement["id"])
    assert first["status"] == "confirmed"
    assert first["published_version"] is None
    assert first["versions"][0]["status"] == "confirmed"
    second = service.get_settlement(draft_settlement["id"])
    assert second["status"] == "draft"
    assert second["published_version"] is None
    assert second["versions"][0]["status"] == "draft"

    detail = client.get(f"/api/settlements/{confirmed_settlement['id']}", headers=admin["headers"]).json()
    assert detail["status"] == "confirmed"
    assert detail["published_version"] is None


def test_list_and_not_found(client, admin):
    create_settlement(client, admin["headers"], "ORDER-LIST-1")
    listed = client.get("/api/settlements?status=draft", headers=admin["headers"])
    assert listed.status_code == 200
    assert any(item["order_code"] == "ORDER-LIST-1" for item in listed.json()["items"])
    assert client.get("/api/settlements/9999", headers=admin["headers"]).status_code == 404
    assert client.get("/api/settlements/9999/audit", headers=admin["headers"]).status_code == 404
