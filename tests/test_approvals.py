import re

import pytest
from fastapi import Request

from core import actions, approvals, dispatch, inbox, outbound, rules
from server.app import email_decision


@pytest.fixture
def mail(monkeypatch, tmp_path):
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    monkeypatch.setenv("SOONY_NOW", "2026-11-22T11:00:00+01:00")
    monkeypatch.setenv("PUBLIC_URL", "https://dockside.test")
    monkeypatch.setenv("MAIL_FROM", "dockside@gmail.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.setitem(rules.CONFIG, "demo_approval_aliases", {"value": True})
    sent = []

    class SMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def starttls(self):
            pass

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr(outbound.smtplib, "SMTP", SMTP)
    return sent


def propose(kind="book_truck", cost=1000, protected=21000):
    return actions.propose("fixture", "Book a truck from CTA Hamburg", "Bring game card cases in before receiving closes.",
                           kind, cost, protected, 120, "Print & Packaging", "Dockside")


def paths(message):
    return re.findall(r"https://dockside.test(/approval/\S+)", message.get_body().get_content())


def request(path, method="GET"):
    _, _, token, decision = path.split("/")
    response = email_decision(token, decision, Request({"type": "http", "method": method}))
    return response, response.body.decode()


def test_scheduled_approval_sends_one_email_and_uses_post(mail):
    a = propose()
    assert a["status"] == "scheduled"
    assert a["approver_name"] == "Paul Winter"
    approvals.email_open()
    approvals.email(a["id"])
    msg, = mail
    assert msg["To"] == "dockside+paul.winter@gmail.com"
    assert msg["Subject"] == "Dockside approval: Book a truck from CTA Hamburg"
    body = msg.get_body().get_content()
    assert "Bring game card cases in before receiving closes." in body
    assert "€1,000.00" in body and "€21,000.00" in body and "120" in body
    assert "22 Nov 2026, 11:15 +0100 unless you stop it" in body
    approve, stop = paths(msg)
    assert approve.endswith("/approve") and stop.endswith("/reject")
    a = actions.get(a["id"])
    recorded, = a["messages"]
    assert recorded["status"] == "sent" and recorded["channel"] == "approval_email"
    assert approve not in recorded["body"] and stop not in recorded["body"]
    assert a["history"][-1]["entry"] == "Emailed Paul Winter for approval"
    with actions.tx() as c:
        inbox.init_schema(c)
        assert dispatch.readable_activity(c)["items"][0]["text"] == "Emailed Paul Winter for approval"

    preview, page = request(approve)
    assert preview.status_code == 200 and '<form method="post">' in page
    assert preview.headers["cache-control"] == "no-store"
    assert actions.get(a["id"])["status"] == "scheduled"
    assert "was approved" in request(approve, "POST")[1]
    assert "Decision already recorded" in request(approve, "POST")[1]
    assert "Decision already recorded" in request(stop, "POST")[1]
    assert actions.get(a["id"])["status"] == "sent"
    assert request("/approval/unknown/approve", "POST")[0].status_code == 404


def test_paid_signature_stop_link_and_staff_override(mail, monkeypatch):
    monkeypatch.setitem(rules.CONFIG, "staff_email", {"value": {"Paul Winter": "paul@example.test"}})
    a = propose(cost=1000, protected=1000)
    assert a["status"] == "proposed"
    approvals.email_open()
    msg, = mail
    assert msg["To"] == "paul@example.test"
    assert "waits for explicit approval" in msg.get_body().get_content()
    _, stop = paths(msg)
    assert "was stopped" in request(stop, "POST")[1]
    assert actions.get(a["id"])["status"] == "rejected"


def test_window_requires_email_then_expires(mail, monkeypatch):
    a = propose()
    monkeypatch.setenv("SOONY_NOW", "2026-11-22T12:00:00+01:00")
    assert actions.due_scheduled() == []
    approvals.email_open()
    msg, = mail
    assert "22 Nov 2026, 12:15 +0100" in msg.get_body().get_content()
    monkeypatch.setenv("SOONY_NOW", "2026-11-22T12:15:00+01:00")
    approve, stop = paths(msg)
    assert request(approve)[0].status_code == 410
    assert request(stop, "POST")[0].status_code == 410
    assert [decision["id"] for decision in actions.due_scheduled()] == [a["id"]]


def test_free_actions_skip_approval_including_open_legacy_actions(mail):
    slot = propose(kind="dock_slot", cost=0)
    assert slot["status"] == "auto"
    release = propose(kind="quality_release", cost=0)
    assert release["status"] == "auto"
    actions.set_status(slot["id"], "scheduled", "legacy")
    assert [a["id"] for a in actions.release_free()] == [slot["id"]]
    approvals.email_open()
    assert mail == []


def test_joint_email_links_are_independent_and_wait_for_every_signer(mail, monkeypatch):
    monkeypatch.setitem(rules.CONFIG, "approval_by_kind",
                        {"value": {"book_truck": ["Site Lead DC-HAM (dock slots)"]}})
    monkeypatch.setitem(rules.CONFIG, "staff_email",
                        {"value": {"Katrin Jansen": "katrin@example.test", "Paul Winter": "paul@example.test"}})
    a = propose()
    approvals.email(a["id"])
    assert {m["To"] for m in mail} == {"katrin@example.test", "paul@example.test"}
    first, second = [paths(m)[0] for m in mail]
    assert first != second
    assert "Waiting for Paul Winter" in request(first, "POST")[1]
    assert "Decision already recorded" in request(first, "POST")[1]
    assert actions.get(a["id"])["status"] == "proposed"
    assert "was approved" in request(second, "POST")[1]
    assert set(actions.get(a["id"])["signatures"]) == {"Katrin Jansen", "Paul Winter"}


def test_joint_signers_cannot_share_demo_mailbox(mail, monkeypatch):
    monkeypatch.setitem(rules.CONFIG, "approval_by_kind",
                        {"value": {"book_truck": ["Site Lead DC-HAM (dock slots)"]}})
    monkeypatch.setitem(rules.CONFIG, "staff_email",
                        {"value": {"Katrin Jansen": "demo+katrin@gmail.com", "Paul Winter": "d.emo+paul@gmail.com"}})
    approvals.email(propose()["id"])
    assert mail == []
