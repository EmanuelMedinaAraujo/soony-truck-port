"""Dockside on Discord: decisions come to your phone, you approve with one tap, and you can ask questions.

Runs inside the server process (same action log as the web UI). Enabled when DISCORD_TOKEN and DISCORD_CHANNEL_ID are set.
- New / escalated decision -> message with "Approve and send" / "Decline" buttons.
- /ask <question>  -> the agent answers with numbers from the tools.
- /answer <question> <text> -> answer a specific open question assigned to you.
- /status          -> money at risk and the decisions waiting.
- Data drop        -> one-line note in the channel.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import traceback

import discord
from discord import app_commands

from core import actions, release, agent, inbox, rules

TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID") or 0)                       # decisions with buttons, /ask, /status
UPDATES_ID = int(os.environ.get("DISCORD_UPDATES_CHANNEL_ID") or 0) or CHANNEL_ID   # automatic updates only (never read)
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID") or 0)
UI_URL = os.environ.get("PUBLIC_URL", "")

eur = lambda v: f"€{round(v or 0):,}"


def open_questions(who):
    """The caller's answerable questions and tasks, from the same source as the dashboard (superseded ones excluded)."""
    from core import questions
    out = {}
    for q in questions.current():
        if q["person"] == who:
            out[q["id"]] = ((f"{q['part']} · " if q.get("part") else "") + q["text"])[:100]
    return out


def answer_route():
    if release.setting("staff_accounts", {}) or release.setting("demo_local_operator", None):
        dashboard = f" at {UI_URL}" if UI_URL else ""
        return f"Open Actions in the Dockside dashboard{dashboard} and answer the question there."
    return "Ask your Dockside administrator to link your Discord account."


def decision_embed(a: dict) -> discord.Embed:
    colour = {"proposed": 0xF2B300, "escalated": 0xC8102E, "approved": 0x1C7A68, "sent": 0x1C7A68, "rejected": 0x5A6A72}.get(a["status"], 0x5A6A72)
    head = f"{eur(a['cost_eur'])} protects {eur(a['eur_protected'])}" if a["eur_protected"] else f"Costs {eur(a['cost_eur'])}"
    e = discord.Embed(title=a["title"][:256], description=a["rationale"][:1500], colour=colour, url=UI_URL or None)
    e.add_field(name="Money", value=head, inline=True)
    e.add_field(name="Needs", value=f"{a['approver_name']} ({a['approver_role']})"[:1024], inline=True)
    if a["ce_copies"]:
        e.add_field(name="Collector's copies", value=f"{a['ce_copies']:,}", inline=True)
    state = release.decision_state(a)
    terms = release.terms_for(a)
    if terms and not release.proposal_blocker(a):
        _, instruction = release.render_instruction(terms, bool(a["payload"].get("repeat_commitment")))
        for start in range(0, len(instruction), 900):
            e.add_field(name="Shipment instruction" if start == 0 else "Shipment instruction (continued)",
                        value=instruction[start:start + 900], inline=False)
    elif a["messages"] and not release.applies(a):
        e.add_field(name="Sends on approval", value="\n".join(f"{m['channel']} to {m['to_name']}: {m['subject']}" for m in a["messages"])[:1024], inline=False)
    if state["terminal"] or state["held_reason"]:
        detail = state["terminal"] or " · ".join(filter(None, (state["held_reason"], state["hold_owner"], state["hold_clears"])))
        e.add_field(name="Release status", value=detail[:1024], inline=False)
    elif a["status"] not in ("proposed",):
        e.set_footer(text=a["history"][-1]["entry"][:200] if a["history"] else a["status"])
    return e


class DecisionView(discord.ui.View):
    def __init__(self, action: dict):
        super().__init__(timeout=None)
        scheduled = action["status"] == "scheduled"
        enabled = release.decision_state(action)["can_approve"]
        aid = action["id"]
        digest = release.approval_digest(action)
        labels = (("Do it now", discord.ButtonStyle.success, "approve"), ("Stop", discord.ButtonStyle.danger, "reject")) if scheduled else \
                 (("Approve and send", discord.ButtonStyle.success, "approve"), ("Decline", discord.ButtonStyle.secondary, "reject"))
        for label, style, verb in labels:
            suffix = ":" + digest if digest else ""
            self.add_item(discord.ui.Button(label=label, style=style, custom_id=f"dock:{verb}:{aid}{suffix}", disabled=not enabled))


class Bot(discord.Client):
    def __init__(self, decide_fn):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.decide_fn = decide_fn
        self.ready = threading.Event()

        @self.tree.command(name="ask", description="Ask Dockside about shipments, parts or what-ifs")
        async def ask(inter: discord.Interaction, question: str):
            await inter.response.defer(thinking=True)
            try:
                ans = await asyncio.to_thread(agent.chat, question)
            except Exception as e:
                ans = str(e) if type(e).__name__ == "LLMUnavailable" else f"Dockside could not answer: {type(e).__name__}. Try again, or ask in the web control room."
            await inter.followup.send(f"**{question}**\n{ans}"[:1990])

        @self.tree.command(name="status", description="Money at risk and decisions waiting")
        async def status(inter: discord.Interaction):
            r = await asyncio.to_thread(rules.evaluate)
            head = next(f for f in r["findings"] if f["id"] == "ce-at-risk")
            pending = [a for a in actions.list_actions(("proposed", "escalated"))]
            lines = [f"**{eur(head['eur_at_risk'])}** in Collector's refunds at risk ({head['ce_copies']:,} copies)."]
            lines += [f"• {a['title']} — needs {a['approver_name']}" for a in pending[:8]] or ["No decisions waiting."]
            await inter.response.send_message("\n".join(lines)[:1990])

        @self.tree.command(name="answer", description="Answer a specific Dockside question")
        @app_commands.describe(question="Select the question you want to answer")
        async def answer(inter: discord.Interaction, question: str, text: str):
            who = release.setting("staff_discord_accounts", {}).get(str(inter.user.id))
            if not who:
                await inter.response.send_message(f"This Discord account has no staff mapping. {answer_route()}", ephemeral=True)
                return
            from core import questions as staff_questions
            target = await asyncio.to_thread(staff_questions.find, question)
            if (not target or target.get("superseded") or target.get("status", "open") != "open"
                    or target["person"] != who):
                await inter.response.send_message("Select an open question assigned to you; replaced questions can't be answered.", ephemeral=True)
                return
            if not (text := text.strip()):
                await inter.response.send_message("Write an answer first.", ephemeral=True)
                return
            try:
                await asyncio.to_thread(staff_questions.answer, target, who, text[:4000], channel="Discord")
            except (KeyError, ValueError):
                await inter.response.send_message("That question is no longer open. Select a current question.", ephemeral=True)
                return
            await inter.response.send_message(f"Thanks {who}. Dockside recorded your answer to {question}.", ephemeral=True)

        @answer.autocomplete("question")
        async def question_choices(inter: discord.Interaction, current: str):
            who = release.setting("staff_discord_accounts", {}).get(str(inter.user.id))
            questions = await asyncio.to_thread(open_questions, who) if who else {}
            query = current.casefold()
            return [app_commands.Choice(name=label, value=mid) for mid, label in questions.items()
                    if query in label.casefold() or query in mid.casefold()][:25]

        @self.tree.command(name="decisions", description="Show every decision waiting for approval, with buttons")
        async def decisions(inter: discord.Interaction):
            pending = actions.list_actions(("proposed", "escalated", "scheduled"))
            if not pending:
                await inter.response.send_message("No decisions waiting. Dockside will post here when something needs you.")
                return
            await inter.response.send_message(f"{len(pending)} decision(s) waiting:")
            for a in pending:
                await inter.followup.send(content=f"For **{a['approver_name']}**", embed=decision_embed(a), view=DecisionView(a))

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID) if GUILD_ID else None
        if guild:
            self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self):
        self.ready.set()
        print(f"[discord] logged in as {self.user}", flush=True)

    async def on_interaction(self, inter: discord.Interaction):
        cid = (inter.data or {}).get("custom_id", "") if inter.type == discord.InteractionType.component else ""
        if not cid.startswith("dock:"):
            return
        _, verb, aid, *revision = cid.split(":", 3)
        who = release.setting("staff_discord_accounts", {}).get(str(inter.user.id))
        if not who:
            await inter.response.send_message("This Discord account is not assigned to a Dockside staff member.", ephemeral=True)
            return
        try:
            await inter.response.defer()
            a = await asyncio.to_thread(self.decide_fn, aid, verb, who, source="Discord",
                                        commitment_digest=revision[0] if revision else None)
            waiting = a["status"] in {"proposed", "escalated", "scheduled"} and not a["held_reason"]
            detail = (f"Held: {a['held_reason']}" if a["held_reason"] else
                      f"Signed by {who}. Waiting for the remaining approvals." if waiting else
                      f"Decision recorded for {who}. Status: {a['status']}.")
            await inter.edit_original_response(embed=decision_embed(a), view=DecisionView(a))
            await inter.followup.send(detail, ephemeral=True)
        except Exception as e:
            await inter.followup.send(f"Could not record that: {e}. Refresh the decision in the control room.", ephemeral=True)

    async def post_decision(self, a: dict):
        ch = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
        if a["status"] == "scheduled":
            await ch.send(content=f"Dockside will do this at {a['next_check_at'][11:16]} unless **{a['approver_name']}** stops it",
                          embed=decision_embed(a), view=DecisionView(a))
            return
        prefix = "Escalated to you: " if a["status"] == "escalated" else ""
        state = release.decision_state(a)
        content = f"{prefix}Signature needed from **{a['approver_name']}**" if state["can_approve"] else None
        await ch.send(content=content, embed=decision_embed(a), view=DecisionView(a))

    async def post_text(self, text: str, main: bool = False):
        cid = CHANNEL_ID if main else UPDATES_ID
        ch = self.get_channel(cid) or await self.fetch_channel(cid)
        await ch.send(text[:1990])


def start(listeners: list, decide_fn) -> Bot | None:
    if not (TOKEN and CHANNEL_ID):
        return None
    bot = Bot(decide_fn)
    loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(bot.start(TOKEN))
        except Exception:
            traceback.print_exc()

    threading.Thread(target=run, daemon=True, name="discord").start()

    posting_escalations = set()

    async def post_chase_escalation(m):
        if m["id"] in posting_escalations:
            return
        posting_escalations.add(m["id"])
        try:
            with actions.tx() as c:
                pending = c.execute("SELECT status FROM messages WHERE id=?", (m["id"],)).fetchone()
            if pending and pending["status"] == "draft":
                text = f"**{m['subject']}**\nFor **{m['to_name']}**\n{m['body']}"
                await bot.post_text(discord.utils.escape_mentions(text), main=True)
                with actions.tx() as c:
                    c.execute("UPDATE messages SET status='delivered',sent_at=? WHERE id=?",
                              (rules.now().isoformat(timespec="seconds"), m["id"]))
                actions.log_event("ping", m["action_id"], f"Discord escalation to {m['to_name']}: {m['subject']}")
        except discord.DiscordException as exc:
            actions.log_event("dispatch_error", m["action_id"], f"Discord escalation: {type(exc).__name__}")
        finally:
            posting_escalations.remove(m["id"])

    def on_event(ev: dict):
        if not bot.ready.is_set():
            return
        if ev["kind"] in ("action_proposed", "action_escalated", "action_scheduled", "action_held"):
            asyncio.run_coroutine_threadsafe(bot.post_decision(ev["action"]), loop)
        elif ev["kind"] == "action_auto":
            a = ev["action"]
            if a["kind"] == "chase_escalation":
                if m := next((m for m in a["messages"] if m["channel"] == "discord" and m["status"] == "draft"), None):
                    asyncio.run_coroutine_threadsafe(post_chase_escalation(m), loop)
                    return
            to = ", ".join(sorted({m["to_name"] for m in a["messages"]})) or "logged"
            reminders = [m["body"] for m in a["messages"]
                         if m["channel"] == "discord" and m["subject"].startswith("Reminder of task task:")]
            text = "\n".join(reminders) if reminders else f"Done by Dockside: **{a['title']}** ({to}). {a['rationale']}"
            asyncio.run_coroutine_threadsafe(bot.post_text(text[:1990]), loop)
        elif ev["kind"] == "action_executed":
            a = ev["action"]
            asyncio.run_coroutine_threadsafe(bot.post_text(f"Executed: **{a['title']}**. Nobody stopped it, so Dockside sent it."), loop)
        elif ev["kind"] == "reply":
            r = ev["reply"]
            asyncio.run_coroutine_threadsafe(bot.post_text(f"Reply from **{r['from_name']}**: {r['subject']}. Dockside is reading it."), loop)
        elif ev["kind"] == "llm_down":
            asyncio.run_coroutine_threadsafe(bot.post_text(f"Agent paused: {ev['message']}", main=True), loop)
        elif ev["kind"] == "data_drop":
            names = ", ".join(p.split("/")[-1] for p in ev["paths"][:5])
            asyncio.run_coroutine_threadsafe(bot.post_text(f"New data on the portal: {names}. Re-planning now."), loop)

    listeners.append(on_event)
    return bot
