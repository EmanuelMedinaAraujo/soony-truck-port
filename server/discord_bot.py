"""Dockside on Discord: decisions come to your phone, you approve with one tap, and you can ask questions.

Runs inside the server process (same action log as the web UI). Enabled when DISCORD_TOKEN and DISCORD_CHANNEL_ID are set.
- New / escalated decision -> message with "Approve and send" / "Decline" buttons.
- /ask <question>  -> the agent answers with numbers from the tools.
- /status          -> money at risk and the decisions waiting.
- Data drop        -> one-line note in the channel.
"""
from __future__ import annotations

import asyncio
import os
import threading
import traceback

import discord
from discord import app_commands

from core import actions, agent, rules

TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID") or 0)
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID") or 0)
UI_URL = os.environ.get("PUBLIC_URL", "")

eur = lambda v: f"€{round(v or 0):,}"


def decision_embed(a: dict) -> discord.Embed:
    colour = {"proposed": 0xF2B300, "escalated": 0xC8102E, "approved": 0x1C7A68, "sent": 0x1C7A68, "rejected": 0x5A6A72}.get(a["status"], 0x5A6A72)
    head = f"{eur(a['cost_eur'])} protects {eur(a['eur_protected'])}" if a["eur_protected"] else f"Costs {eur(a['cost_eur'])}"
    e = discord.Embed(title=a["title"][:256], description=a["rationale"][:1500], colour=colour, url=UI_URL or None)
    e.add_field(name="Money", value=head, inline=True)
    e.add_field(name="Needs", value=f"{a['approver_name']} ({a['approver_role']})"[:1024], inline=True)
    if a["ce_copies"]:
        e.add_field(name="Collector's copies", value=f"{a['ce_copies']:,}", inline=True)
    if a["messages"]:
        e.add_field(name="Sends on approval", value="\n".join(f"{m['channel']} to {m['to_name']}: {m['subject']}" for m in a["messages"])[:1024], inline=False)
    if a["status"] not in ("proposed",):
        e.set_footer(text=a["history"][-1]["entry"][:200] if a["history"] else a["status"])
    return e


class DecisionView(discord.ui.View):
    def __init__(self, aid: str, enabled: bool = True, scheduled: bool = False):
        super().__init__(timeout=None)
        labels = (("Do it now", discord.ButtonStyle.success, "approve"), ("Stop", discord.ButtonStyle.danger, "reject")) if scheduled else \
                 (("Approve and send", discord.ButtonStyle.success, "approve"), ("Decline", discord.ButtonStyle.secondary, "reject"))
        for label, style, verb in labels:
            self.add_item(discord.ui.Button(label=label, style=style, custom_id=f"dock:{verb}:{aid}", disabled=not enabled))


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
                ans = f"Dockside could not answer: {type(e).__name__}. Try again, or ask in the web control room."
            await inter.followup.send(f"**{question}**\n{ans}"[:1990])

        @self.tree.command(name="status", description="Money at risk and decisions waiting")
        async def status(inter: discord.Interaction):
            r = await asyncio.to_thread(rules.evaluate)
            head = next(f for f in r["findings"] if f["id"] == "ce-at-risk")
            pending = [a for a in actions.list_actions(("proposed", "escalated"))]
            lines = [f"**{eur(head['eur_at_risk'])}** in Collector's refunds at risk ({head['ce_copies']:,} copies)."]
            lines += [f"• {a['title']} — needs {a['approver_name']}" for a in pending[:8]] or ["No decisions waiting."]
            await inter.response.send_message("\n".join(lines)[:1990])

        @self.tree.command(name="decisions", description="Show every decision waiting for approval, with buttons")
        async def decisions(inter: discord.Interaction):
            pending = actions.list_actions(("proposed", "escalated", "scheduled"))
            if not pending:
                await inter.response.send_message("No decisions waiting. Dockside will post here when something needs you.")
                return
            await inter.response.send_message(f"{len(pending)} decision(s) waiting:")
            for a in pending:
                await inter.followup.send(content=f"For **{a['approver_name']}**", embed=decision_embed(a), view=DecisionView(a["id"], scheduled=a["status"] == "scheduled"))

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
        _, verb, aid = cid.split(":", 2)
        who = inter.user.display_name
        try:
            a = await asyncio.to_thread(self.decide_fn, aid, verb, f"{who} (Discord)")
            done = "Done" if verb == "approve" else "Stopped"
            recipients = ", ".join(sorted({m["to_name"] for m in a["messages"]})) if verb == "approve" else ""
            await inter.response.edit_message(embed=decision_embed(a), view=DecisionView(aid, enabled=False))
            await inter.followup.send(f"{done}{' to ' + recipients if recipients else ''}. Logged for {who}.", ephemeral=True)
        except Exception as e:
            await inter.response.send_message(f"Could not record that: {e}. Refresh the decision in the control room.", ephemeral=True)

    async def post_decision(self, a: dict):
        ch = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
        if a["status"] == "scheduled":
            await ch.send(content=f"Dockside will do this at {a['next_check_at'][11:16]} unless **{a['approver_name']}** stops it",
                          embed=decision_embed(a), view=DecisionView(a["id"], scheduled=True))
            return
        prefix = "Escalated to you: " if a["status"] == "escalated" else ""
        await ch.send(content=f"{prefix}Signature needed from **{a['approver_name']}**", embed=decision_embed(a), view=DecisionView(a["id"]))

    async def post_text(self, text: str):
        ch = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
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

    def on_event(ev: dict):
        if not bot.ready.is_set():
            return
        if ev["kind"] in ("action_proposed", "action_escalated", "action_scheduled"):
            asyncio.run_coroutine_threadsafe(bot.post_decision(ev["action"]), loop)
        elif ev["kind"] == "action_auto":
            a = ev["action"]
            to = ", ".join(sorted({m["to_name"] for m in a["messages"]})) or "logged"
            asyncio.run_coroutine_threadsafe(bot.post_text(f"Done by Dockside: **{a['title']}** ({to}). {a['rationale']}"), loop)
        elif ev["kind"] == "action_executed":
            a = ev["action"]
            asyncio.run_coroutine_threadsafe(bot.post_text(f"Executed: **{a['title']}**. Nobody stopped it, so Dockside sent it."), loop)
        elif ev["kind"] == "reply":
            r = ev["reply"]
            asyncio.run_coroutine_threadsafe(bot.post_text(f"Reply from **{r['from_name']}**: {r['subject']}. Dockside is reading it."), loop)
        elif ev["kind"] == "data_drop":
            names = ", ".join(p.split("/")[-1] for p in ev["paths"][:5])
            asyncio.run_coroutine_threadsafe(bot.post_text(f"New data on the portal: {names}. Re-planning now."), loop)

    listeners.append(on_event)
    return bot
