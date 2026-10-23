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


def _invoice(inv_id="I1", client="Acme", email="acme@example.com", due_in=-1, amount=300.0):
    db.save_invoice({"id": inv_id, "number": inv_id, "client_name": client, "client_email": email,
                     "amount": amount, "currency": "USD", "status": "SENT",
                     "due_date": (date.today() + timedelta(days=due_in)).isoformat(),
                     "items": [{"name": "Work", "quantity": 1, "unit_price": amount}]})


def _clock(days):
    db.set_setting("demo_offset_days", days)


def test_not_overdue_means_no_reminder(reminders):
    _invoice(due_in=3)
    assert chasing.chase_due() == [] and not reminders


def test_friendly_then_firm_then_final_then_weekly_follow_ups(reminders):
    _invoice(due_in=0)
    _clock(1); chasing.chase_due()
    _clock(2); chasing.chase_due()                       # same stage: nothing new
    _clock(7); chasing.chase_due()
    _clock(14); chasing.chase_due()
    _clock(18); chasing.chase_due()                      # too soon after the final
    _clock(21); chasing.chase_due()
    subjects = [s for _, s, _ in reminders]
    assert [s.split(":")[0] for s in subjects] == ["Friendly reminder", "Second reminder",
                                                     "Final notice", "Still unpaid"]
    assert db.get_invoice("I1")["chase_stage"] == 4


def test_missed_stages_are_skipped_not_sent_back_to_back(reminders):
    _invoice(due_in=0)
    _clock(20); chasing.chase_due()
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
    _clock(14); chasing.chase_due()
    assert not db.get_invoice("I1")["needs_you_at"]
    _clock(21); chasing.chase_due()
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
