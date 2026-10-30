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


def _sent(reminders):
    return [s.split(":")[0] for _, s, _ in reminders]


def _clock(days):
    db.set_setting("demo_offset_days", days)


def test_no_reminder_on_the_day_it_is_sent(reminders):
    _invoice(due_in=3, term=3)
    assert chasing.chase_due() == [] and not reminders


def test_friendly_every_day_until_due_then_every_3_days_and_stricter(reminders):
    _invoice(due_in=3, term=30)
    overdue_days = []
    for day in range(0, 35):
        _clock(day)
        before = len(reminders)
        chasing.chase_due(); chasing.chase_due()                 # twice a day: still one
        overdue_days += [day - 3] * (len(reminders) - before)
    assert overdue_days == [-3, -2, -1, 0, 1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 31]
    assert _sent(reminders) == (["Friendly reminder"] * 4 + ["Reminder"] * 3 + ["Urgent"] * 4
                                + ["Final notice"] * 4)
    assert reminders[4][1] == "Reminder: invoice #I1 is 1 day overdue"
    assert reminders[-1][1] == "Final notice: invoice #I1 is 31 days overdue"
    assert "14 earlier reminders" in reminders[-1][2]
    assert db.get_invoice("I1")["chase_stage"] == 15


def test_next_reminder_follows_the_schedule(reminders):
    _invoice(due_in=2)
    assert chasing.next_step(db.get_invoice("I1")) == ("friendly", date.today())
    chasing.chase_due()
    assert chasing.next_step(db.get_invoice("I1")) == ("friendly", date.today() + timedelta(days=1))
    _clock(2); chasing.chase_due()                               # the due date
    assert chasing.next_step(db.get_invoice("I1")) == ("firm", date.today() + timedelta(days=3))
    _clock(3); chasing.chase_due()                               # the day after it
    assert chasing.next_step(db.get_invoice("I1")) == ("firm", date.today() + timedelta(days=6))


def test_friendly_reminders_before_the_due_date_do_not_say_it_is_late(reminders):
    _invoice(due_in=2)
    chasing.chase_due()
    _, subject, note = reminders[0]
    assert subject == "Friendly reminder: invoice #I1"
    assert "is due on" in note and "overdue" not in note
    assert "due in 2 days" in db.list_events()[0]["message"]
    assert "is due today" in chasing.write_reminder(db.get_invoice("I1"), "friendly", 0)


def test_turning_the_demo_clock_back_does_not_stall_chasing(reminders):
    _invoice(due_in=0)
    _clock(40); chasing.chase_due()                       # 40 days overdue
    _clock(22)                                            # then back: that day is now the future
    assert chasing.next_step(db.get_invoice("I1")) == ("final", date.today() + timedelta(days=22))
    chasing.chase_due(); chasing.chase_due()              # today's goes out, once
    assert len(reminders) == 2
    assert chasing.next_step(db.get_invoice("I1"))[1] == date.today() + timedelta(days=25)


def test_missed_reminders_are_skipped_not_sent_back_to_back(reminders):
    _invoice(due_in=0)
    _clock(23); chasing.chase_due(); chasing.chase_due()
    assert len(reminders) == 1 and reminders[0][1].startswith("Final notice")
    _clock(25); chasing.chase_due()
    assert len(reminders) == 1                            # 3 days from the last one, not day 25
    _clock(26); chasing.chase_due()
    assert len(reminders) == 2


def test_pause_and_dont_chase_are_respected(reminders):
    _invoice("I1", "Acme", "acme@example.com", due_in=-5)
    _invoice("I2", "Globex", "globex@example.com", due_in=-5)
    db.set_setting("chasing_paused", True)
    assert chasing.chase_due() == []
    db.set_setting("chasing_paused", False)
    db.set_client_chase("Globex", True)
    chasing.chase_due()
    assert [i for i, _, _ in reminders] == ["I1"]


def test_needs_you_past_the_30_day_mark_and_reminders_continue(reminders):
    _invoice(due_in=0)
    _clock(30); chasing.chase_due()
    assert not db.get_invoice("I1")["needs_you_at"]
    _clock(31); chasing.chase_due()
    assert db.get_invoice("I1")["needs_you_at"]
    sent = len(reminders)
    _clock(34); chasing.chase_due()
    assert len(reminders) == sent + 1


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
