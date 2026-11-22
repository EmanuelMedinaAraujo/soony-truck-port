import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core import actions, rules
from examples.replay_gate import at
from server import discord_bot


def test_answer_binds_to_the_chosen_open_question(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DB", tmp_path / "actions.db")
    monkeypatch.setitem(rules.CONFIG, "staff_discord_accounts", {"value": {"7": "Dr. Maren Vogt"}})

    def deliver(part, subject, recipient):
        action = actions.propose(f"gap-{part}", subject, "", "escalate", 0, 0, 0, None, None,
                                 messages=[{"channel": "discord", "to_name": recipient,
                                            "to_address": None, "language": "en", "subject": subject, "body": subject}])
        message, = action["messages"]
        with actions.tx() as log:
            log.execute("UPDATE messages SET status='delivered',sent_at=? WHERE id=?",
                        (rules.now().isoformat(), message["id"]))
        return message["id"]

    with rules.replay(rules.CONFIG, at("02:25")):
        books = deliver("CMP-GAM-008", "Which art-book PO should we use?", "Dr. Maren Vogt")
    with rules.replay(rules.CONFIG, at("04:17")):
        statue = deliver("CMP-GAM-010", "Where are the approved statues?", "Dr. Maren Vogt")
        other = deliver("CMP-GAM-009", "Who verified the pickup?", "Paul Winter")

    async def answer_question():
        bot = discord_bot.Bot(None)
        command = bot.tree.get_command("answer")
        question = next(parameter for parameter in command.parameters if parameter.name == "question")
        assert question.required and question.autocomplete
        response = AsyncMock()
        inter = SimpleNamespace(user=SimpleNamespace(id=7), response=response)
        choices = await command._params["question"].autocomplete(inter, "")
        assert [choice.value for choice in choices] == [statue, books]
        assert choices[-1].name == "CMP-GAM-008 · Which art-book PO should we use?"
        await command.callback(inter, other, "Wrong recipient")
        await command.callback(inter, books, "Use the emergency reference.")
        await command.callback(inter, books, "Another answer")
        return response

    response = asyncio.run(answer_question())
    assert "Select an open question" in response.send_message.await_args_list[0].args[0]
    assert "Thanks Dr. Maren Vogt" in response.send_message.await_args_list[1].args[0]
    assert "Select an open question" in response.send_message.await_args_list[2].args[0]
    with actions.tx() as log:
        assert [tuple(row) for row in log.execute("SELECT msg_id, body FROM replies")] == [
            (books, "Use the emergency reference."),
        ]


def test_unmapped_discord_answer_lists_only_configured_route(monkeypatch):
    monkeypatch.setitem(rules.CONFIG, "staff_discord_accounts", {"value": {}})
    monkeypatch.setitem(rules.CONFIG, "staff_accounts", {"value": {"staff@example.com": "Staff Member"}})
    monkeypatch.setattr(discord_bot, "UI_URL", "https://dockside.example")

    async def answer_question():
        command = discord_bot.Bot(None).tree.get_command("answer")
        response = AsyncMock()
        await command.callback(SimpleNamespace(user=SimpleNamespace(id=7), response=response), "unused", "answer")
        return response.send_message.await_args.args[0]

    text = asyncio.run(answer_question())
    assert "Actions in the Dockside dashboard at https://dockside.example" in text
    assert "email" not in text.lower()
