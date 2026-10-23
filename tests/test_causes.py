"""Pay the bill, not the person: reading bills, the checks, campaigns, donations to the hospital."""
import io
import json
from datetime import date, timedelta

import pytest

from backend import causes, db

RECENT = (date.today() - timedelta(days=20)).isoformat()
GOOD = {"looks_like_a_medical_bill": True, "provider": "CITY GENERAL HOSPITAL", "patient": "Sam Lee",
        "date": RECENT, "total": 3000.0, "currency": "USD",
        "line_items": [{"name": "Surgery", "amount": 2800.0}, {"name": "Pharmacy", "amount": 200.0}]}
CAMPAIGN = {"id": "C1", "provider_name": "City General Hospital", "patient_name": "Sam Lee",
            "goal": 3000.0, "currency": "USD"}


def _png(color="white"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (3000, 1500), color).save(buf, "PNG")
    return buf.getvalue()


def _failed(checks):
    return {c["key"] for c in checks if not c["ok"]}


# ---------- names and checks ----------

@pytest.mark.parametrize("a,b,same", [
    ("CITY GENERAL HOSPITAL", "City General Hospital", True), ("City General", "City General Hospital", True),
    ("St. Mary's Medical Center", "St Marys", True), ("Sam Lee", "SAM LEE", True), ("Mr. Sam Lee", "Sam Lee", True),
    ("Jordan Smith", "Sam Lee", False), ("Luigi's Pizzeria", "City General Hospital", False), (None, "x", False),
])
def test_names_match(a, b, same):
    assert causes.names_match(a, b) is same


def test_a_matching_bill_passes_every_check():
    checks = causes.run_checks(CAMPAIGN, GOOD, "hash-1")
    assert _failed(checks) == set() and causes.verdict(checks) == "verified" and len(checks) == 7


@pytest.mark.parametrize("change,fails", [
    ({"looks_like_a_medical_bill": False}, {"is_bill"}),
    ({"provider": "Luigi's Pizzeria"}, {"provider"}),
    ({"patient": "Jordan Smith"}, {"patient"}),
    ({"total": 1000.0, "line_items": []}, {"amount"}),
    ({"date": "2019-01-01"}, {"date"}),
    ({"date": (date.today() + timedelta(days=30)).isoformat()}, {"date"}),
    ({"date": None}, {"date"}),
    ({"line_items": [{"name": "Surgery", "amount": 100.0}]}, {"adds_up"}),
])
def test_each_problem_is_caught(change, fails):
    checks = causes.run_checks(CAMPAIGN, {**GOOD, **change}, "hash-1")
    assert _failed(checks) == fails and causes.verdict(checks) == "needs_review"


def test_the_same_bill_cannot_back_two_campaigns():
    db.save_campaign({**CAMPAIGN, "id": "OTHER", "token": "t-other", "title": "x", "story": "y",
                      "provider_email": "h@x.com", "bill_hash": "same-hash", "verdict": "verified"})
    assert "unique" in _failed(causes.run_checks(CAMPAIGN, GOOD, "same-hash"))


def test_initials_hide_the_name():
    assert causes.initials("Sam Lee") == "S. L." and causes.initials("") == "—"


# ---------- reading the bill ----------

def test_images_are_cleaned_up_before_reading():
    from PIL import Image
    data, mime = causes.prepare_image(_png())
    img = Image.open(io.BytesIO(data))
    assert mime == "image/png" and img.mode == "L" and max(img.size) <= 2000


def test_a_non_image_is_rejected():
    with pytest.raises(causes.CauseError, match="readable image"):
        causes.prepare_image(b"not an image")


def test_image_bills_go_to_the_vision_model(fake_llm):
    fake_llm.reply(json.dumps(GOOD))
    bill = causes.read_bill(_png(), "image/png")
    assert bill["total"] == 3000 and bill["patient"] == "Sam Lee"
    content = fake_llm.requests[0]["messages"][0]["content"]
    assert content[1]["type"] == "image_url" and content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert fake_llm.requests[0]["model"] == causes.config.LLM_VISION_MODEL


def _text_pdf(text):
    """A minimal one-page PDF with real text in it."""
    stream = f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)


def test_pdf_bills_are_read_from_their_text(fake_llm):
    fake_llm.reply(json.dumps(GOOD))
    bill = causes.read_bill(_text_pdf("CITY GENERAL HOSPITAL Patient Sam Lee Amount due 3000.00"), "application/pdf")
    assert bill["provider"] == "CITY GENERAL HOSPITAL" and "Amount due 3000.00" in fake_llm.prompt()


def test_scanned_pdf_without_text_asks_for_a_photo(fake_llm):
    with pytest.raises(causes.CauseError, match="scan"):
        causes.read_bill(_text_pdf(""), "application/pdf")


def test_without_the_ai_bills_cannot_be_read():
    with pytest.raises(causes.CauseError, match="needs the AI"):
        causes.read_bill(_png(), "image/png")


# ---------- campaigns through the API ----------

def _create(client, png=None, goal=3000, email="city-hospital@example.com", name="bill.png"):
    return client.post("/api/causes", data={
        "title": "Help Sam", "story": "Emergency surgery.", "patient_name": "Sam Lee",
        "provider_name": "City General Hospital", "provider_email": email, "goal": str(goal)},
        files={"bill": (name, png or _png(), "image/png")})


def test_create_verified_campaign_and_its_views(client, fake_llm, tmp_path, monkeypatch):
    monkeypatch.setattr(causes.config, "BILLS_DIR", tmp_path / "bills")
    fake_llm.reply(json.dumps(GOOD))
    c = _create(client).json()
    assert c["verdict"] == "verified" and c["accepting"] and c["provider_email"] == "city-hospital@example.com"
    assert list((tmp_path / "bills").iterdir()), "the bill is kept privately"
    assert client.get("/api/causes").json()[0]["id"] == c["id"]
    public = causes.view(db.get_campaign(c["id"]), public=True)
    assert "provider_email" not in public and "patient_name" not in public and public["patient"] == "S. L."
    page = client.get(f"/c/{c['token']}").text
    assert "Bill verified" in page and "S. L." in page and "Sam Lee" not in page
    assert "city-hospital@example.com" not in page and "Donate with PayPal" in page


def test_unverified_campaigns_take_no_donations(client, fake_llm, fake_paypal):
    fake_llm.reply(json.dumps({**GOOD, "patient": "Jordan Smith"}))
    c = _create(client).json()
    assert c["verdict"] == "needs_review" and not c["accepting"]
    assert "Donations open once" in client.get(f"/c/{c['token']}").text
    r = client.post(f"/c/{c['token']}/donate", data={"amount": "5"})
    assert r.status_code == 400 and not fake_paypal.orders


def test_unreadable_bill(client, fake_llm):
    fake_llm.reply("I can't read this, sorry.")
    c = _create(client).json()
    assert c["verdict"] == "unreadable" and c["checks"][0]["key"] == "readable"


@pytest.mark.parametrize("kwargs,error", [({"email": "not-an-email"}, "PayPal email"),
                                          ({"goal": 0}, "how much"), ({"name": "bill.exe"}, "photo")])
def test_create_validation(client, fake_llm, kwargs, error):
    if "name" in kwargs:
        r = client.post("/api/causes", data={"title": "t", "story": "s", "patient_name": "p",
                                             "provider_name": "h", "provider_email": "h@x.com", "goal": "5"},
                        files={"bill": ("bill.exe", b"MZ...", "application/octet-stream")})
    else:
        r = _create(client, **kwargs)
    assert r.status_code == 400 and error in r.json()["detail"]


def test_corrected_bill_reverifies(client, fake_llm):
    fake_llm.reply(json.dumps({**GOOD, "patient": "Jordan Smith"}))
    c = _create(client).json()
    fake_llm.reply(json.dumps(GOOD))
    r = client.post(f"/api/causes/{c['id']}/bill", files={"bill": ("fixed.png", _png("ivory"), "image/png")})
    assert r.json()["verdict"] == "verified"


# ---------- donations: straight to the hospital ----------

@pytest.fixture
def verified(client, fake_llm):
    fake_llm.reply(json.dumps(GOOD))
    return _create(client, goal=100).json()


def test_donation_order_pays_the_hospital_and_is_counted_once(client, fake_paypal, verified):
    r = client.post(f"/c/{verified['token']}/donate", data={"amount": "40"}, follow_redirects=False)
    assert r.status_code == 303 and "sandbox.paypal.com" in r.headers["location"]
    order_id, order = next(iter(fake_paypal.orders.items()))
    unit = order["purchase_units"][0]
    assert unit["payee"]["email_address"] == "city-hospital@example.com" and unit["amount"]["value"] == "40.00"
    assert unit["custom_id"] == verified["id"]
    back = client.get(f"/donation/return?token={order_id}", follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == f"/c/{verified['token']}?thanks=40.0"
    client.get(f"/donation/return?token={order_id}", follow_redirects=False)        # refresh: once
    client.post("/webhooks/paypal", json={"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {
        "supplementary_data": {"related_ids": {"order_id": order_id}}}})            # webhook: once
    v = causes.view(db.get_campaign(verified["id"]))
    assert v["raised"] == 40 and v["donors"] == 1 and v["remaining"] == 60
    assert "Thank you! Your USD 40.00 was paid directly to City General Hospital" in \
        client.get(f"/c/{verified['token']}?thanks=40").text


def test_donations_stop_at_the_goal(client, fake_paypal, verified):
    over = client.post(f"/c/{verified['token']}/donate", data={"amount": "150"})
    assert over.status_code == 400 and "Only USD 100.00 is still needed" in over.text
    assert client.post(f"/c/{verified['token']}/donate", data={"amount": "0.5"}).status_code == 400
    client.post(f"/c/{verified['token']}/donate", data={"amount": "100"}, follow_redirects=False)
    order_id = next(iter(fake_paypal.orders))
    client.get(f"/donation/return?token={order_id}", follow_redirects=False)
    assert "Fully funded" in client.get(f"/c/{verified['token']}").text
    assert client.post(f"/c/{verified['token']}/donate", data={"amount": "5"}).status_code == 400


def test_no_bill_changes_after_donations_and_closing(client, fake_paypal, verified, fake_llm):
    client.post(f"/c/{verified['token']}/donate", data={"amount": "10"}, follow_redirects=False)
    client.get(f"/donation/return?token={next(iter(fake_paypal.orders))}", follow_redirects=False)
    fake_llm.reply(json.dumps(GOOD))
    r = client.post(f"/api/causes/{verified['id']}/bill", files={"bill": ("b.png", _png("gray"), "image/png")})
    assert r.status_code == 409
    assert client.post(f"/api/causes/{verified['id']}/close").json()["status"] == "closed"
    assert "closed" in client.get(f"/c/{verified['token']}").text
    assert client.get("/c/nope").status_code == 404 and client.get("/api/causes/nope").status_code == 404
