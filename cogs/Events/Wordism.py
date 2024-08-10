import datetime
import inspect
import random
from enum import StrEnum
from pathlib import Path
from typing import cast

import discord
from discord.ext import commands, tasks

from bot import Elysian

ICONS = "assets/wordism_icons/"


class ModerationFlags(StrEnum):
    NEW_ACCOUNT = "Suspicious Member: Account age less than or equal to 14 days"


class Wordism(commands.Cog):

    def __init__(self, bot: Elysian) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.wordism_icon_change.start()

    async def cog_unload(self) -> None:
        self.wordism_icon_change.stop()

    @commands.Cog.listener("on_member_join")
    async def welcome_event(self, member: discord.Member) -> discord.Message:
        ch = cast(discord.TextChannel, self.bot.guild.get_channel(1262409199992705105))
        if datetime.timedelta(seconds=datetime.datetime.now().timestamp() - member.created_at.timestamp()).days <= 14:
            # await member.kick(reason=ModerationFlags.NEW_ACCOUNT)
            return await ch.send(
                content=inspect.cleandoc(
                    f"""**{member.name}** just joined the server! Unfortunately they've been kicked due to:
                - `{ModerationFlags.NEW_ACCOUNT}`
                -# This message will be deleted in 30 seconds."""
                ),
                delete_after=30.0,
            )
        return await ch.send(content=(f"**{member.name}** just joined the server!"))

    @commands.Cog.listener("on_member_remove")
    async def leave_event(self, member: discord.Member) -> discord.Message:
        # TODO: Add a persistent roles functionality in this event as well as `welcome_event`
        ch = cast(discord.TextChannel, self.bot.guild.get_channel(1262409199992705105))
        return await ch.send(content=f"**{member.name}** left the server. Unfortunate.")

    @tasks.loop(name="wordism_icon_change", hours=1.0)
    async def wordism_icon_change(self) -> None:
        files = list(Path(ICONS).iterdir())

        with Path(random.choice(files)).open(mode="rb") as image:
            await self.bot.guild.edit(icon=image.read())
        return


async def setup(bot: Elysian) -> None:
    await bot.add_cog(Wordism(bot))
