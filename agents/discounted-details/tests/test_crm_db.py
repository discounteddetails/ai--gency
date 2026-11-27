from datetime import timedelta

from app import clock
from app.services.sheets import normalize_phone


async def test_settings_typed(crm):
    s = await crm.get_settings()
    assert s["min_notice_hours"] == 3 and s["human_agent_messenger"] is False
    assert s["payment_methods"].startswith("Cash")


async def test_ids_increment_and_lookup(crm):
    c1 = await crm.create_customer({"first_name": "Maria", "instagram_igsid": "111", "phone": "(512) 555-0182"})
    c2 = await crm.create_customer({"first_name": "Mike", "messenger_psid": "222"})
    assert (c1, c2) == ("C0001", "C0002")
    assert (await crm.find_customer("instagram", "111"))["first_name"] == "Maria"
    assert (await crm.find_customer(phone="512-555-0182"))["customer_id"] == "C0001"
    assert await crm.find_customer("messenger", "999") is None


async def test_vehicle_upsert_dedupes(crm):
    v1 = await crm.upsert_vehicle("C0001", {"year": 2020, "make": "Chevy", "model": "Tahoe", "size_class": "large_suv"})
    v2 = await crm.upsert_vehicle("C0001", {"year": 2020, "make": "chevy", "model": "tahoe", "color": "black"})
    assert v1 == v2 == "V0001"


async def test_dnc_detection(crm):
    cid = await crm.create_customer({"first_name": "X"})
    assert not crm.is_dnc(await crm.get_customer(cid))
    await crm.update_customer(cid, {"do_not_contact": True})
    assert crm.is_dnc(await crm.get_customer(cid))


def test_phone_normalization():
    for raw in ("512-555-0182", "(512) 555 0182", "+1 512 555 0182", "15125550182"):
        assert normalize_phone(raw) == "+15125550182"


def test_dedupe_and_kv(db):
    assert db.mark_event_processed("m1") is True
    assert db.mark_event_processed("m1") is False
    db.kv_set("global_pause", True)
    assert db.kv_get("global_pause") is True
    assert db.next_counter("x") == 1 and db.next_counter("x") == 2


def test_thread_roundtrip(db):
    t = clock.now()
    db.upsert_thread("instagram:1", channel="instagram", sender_id="1", last_inbound_at=t)
    db.upsert_thread("instagram:1", paused_until=t + timedelta(hours=12))
    th = db.get_thread("instagram:1")
    assert th["last_inbound_at"] == t and th["paused_until"] == t + timedelta(hours=12)


def test_history_whole_turns(db):
    for i in range(5):
        db.append_history("t", f"turn{i}", "user", [{"type": "text", "text": f"u{i}"}])
        db.append_history("t", f"turn{i}", "assistant", [{"type": "text", "text": f"a{i}"}])
    h = db.load_history("t", limit_turns=2)
    assert len(h) == 4 and h[0]["content"][0]["text"] == "u3"
