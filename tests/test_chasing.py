"""Chasing until paid: the right reminder at the right time, never spam, hand-off to you."""
from datetime import date, timedelta

import pytest

from backend import chasing, db
from backend.paypal import invoices as pp_invoices


@pytest.fixture
def reminders(monkeypatch):
    sent = []
    monkeypatch.setattr(pp_invoices, "remind",
                        lambda inv_id, note, subject=None: sent.append((inv_id, subject, note)))
    return sent


def _invoice(inv_id="I1", client="Acme", email="acme@example.com", due_in=-1, amount=300.0,
             term=30):
    """term: days between sending the invoice and its due date."""
    due = date.today() + timedelta(days=due_in)
    db.save_invoice({"id": inv_id, "number": inv_id, "client_name": client, "client_email": email,
                     "amount": amount, "currency": "USD", "status": "SENT",
                     "due_date": due.isoformat(),
                     "items": [{"name": "Work", "quantity": 1, "unit_price": amount}]})
    db.update_invoice(inv_id, created_at=(due - timedelta(days=term)).isoformat() + "T09:00:00+00:00")


def _clock(days):
    db.set_setting("demo_offset_days", days)


def test_not_overdue_means_no_reminder(reminders):
    _invoice(due_in=3)
    assert chasing.chase_due() == [] and not reminders


def test_a_reminder_every_day_friendly_for_a_week_then_escalating(reminders):
    _invoice(due_in=0)
    for day in range(1, 31):
        _clock(day); chasing.chase_due(); chasing.chase_due()   # twice a day: still one
    tones = [s.split(":")[0] for _, s, _ in reminders]
    assert len(tones) == 30
    assert tones[:7] == ["Friendly reminder"] * 7
    assert tones[7:14] == ["Reminder"] * 7 and tones[14:21] == ["Urgent"] * 7
    assert tones[21:] == ["Final notice"] * 9
    assert reminders[-1][1] == "Final notice: invoice #I1 is 30 days overdue"
    assert "29 earlier reminders" in reminders[-1][2]
    inv = db.get_invoice("I1")
    assert inv["chase_stage"] == 30
    assert inv["final_day"] == (date.today() + timedelta(days=22)).isoformat()


def test_next_reminder_is_tomorrow_once_today_is_sent(reminders):
    _invoice(due_in=0)
    assert chasing.next_step(db.get_invoice("I1")) == ("friendly", date.today() + timedelta(days=1))
    _clock(7); chasing.chase_due()
    assert chasing.next_step(db.get_invoice("I1")) == ("firm", date.today() + timedelta(days=8))


def test_short_deadlines_are_urgent_from_day_one_then_final(reminders):
    _invoice(due_in=0, term=2)                           # "due in 2 days"
    assert chasing.next_step(db.get_invoice("I1"))[0] == "urgent"
    for day in range(1, 5):
        _clock(day); chasing.chase_due()
    subjects = [s.split(":")[0] for _, s, _ in reminders]
    assert subjects == ["Urgent", "Urgent", "Final notice", "Final notice"]
    assert "needed urgently" in reminders[0][2]
    assert db.get_invoice("I1")["final_day"] == (date.today() + timedelta(days=3)).isoformat()


def test_a_week_is_still_short_but_eight_days_is_not():
    _invoice("I1", due_in=0, term=7)
    _invoice("I2", due_in=0, term=8)
    assert chasing.stages_for(db.get_invoice("I1")) == chasing.SHORT_STAGES
    assert chasing.stages_for(db.get_invoice("I2")) == chasing.STAGES


def test_missed_days_are_skipped_not_sent_back_to_back(reminders):
    _invoice(due_in=0)
    _clock(23); chasing.chase_due(); chasing.chase_due()
    assert len(reminders) == 1 and reminders[0][1].startswith("Final notice")


def test_pause_and_dont_chase_are_respected(reminders):
    _invoice("I1", "Acme", "acme@example.com", due_in=-5)
    _invoice("I2", "Globex", "globex@example.com", due_in=-5)
    db.set_setting("chasing_paused", True)
    assert chasing.chase_due() == []
    db.set_setting("chasing_paused", False)
    db.set_client_chase("Globex", True)
    chasing.chase_due()
    assert [i for i, _, _ in reminders] == ["I1"]


def test_needs_you_a_week_after_the_final_notice(reminders):
    _invoice(due_in=0)
    _clock(22); chasing.chase_due()
    assert not db.get_invoice("I1")["needs_you_at"]
    _clock(29); chasing.chase_due()
    assert db.get_invoice("I1")["needs_you_at"]


def test_paid_invoices_stop_being_chased_and_note_is_posted_once(reminders):
    _invoice(due_in=-10)
    assert chasing.mark_paid("I1") is True
    assert chasing.mark_paid("I1") is False
    _clock(30); chasing.chase_due()
    assert not reminders
    notes = [e for e in db.list_events() if e["event_type"] == "UPSTREAM.INVOICE.PAID"]
    assert len(notes) == 1 and "Set aside" in notes[0]["message"]


def test_template_reminder_without_ai_mentions_what_is_owed():
    _invoice(amount=450)
    note = chasing.write_reminder(db.get_invoice("I1"), "final", 15)
    assert "450.00" in note and "final" in note.lower()


def test_demo_clock_never_applies_in_live_mode(monkeypatch):
    from backend import config
    db.set_setting("demo_offset_days", 30)
    monkeypatch.setattr(config, "PAYPAL_BASE", "https://api-m.paypal.com")
    assert chasing.demo_offset_days() == 0
