from __future__ import annotations

import datetime
import functools
import logging
from itertools import product
from pathlib import Path
from pkgutil import iter_modules
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self, overload

import aiohttp
import asyncpg
import discord
import jishaku
import mystbin
from discord.ext import commands

from utils import (
    BASE_PREFIX,
    BOT_TOKEN,
    DESCRIPTION,
    OWNERS_ID,
    POSTGRES_CREDENTIALS,
    SERVER_INVITE,
    THEME_COLOUR,
    WEBHOOK_URL,
    AlreadyBlacklisted,
    BlacklistBase,
    BlacklistedGuild,
    BlacklistedUser,
    LagContext,
    NotBlacklisted,
    PrefixAlreadyPresent,
    PrefixNotInitialised,
    PrefixNotPresent,
    UnderMaintenance,
)
from utils.errors import FeatureDisabled

if TYPE_CHECKING:
    from discord.abc import Snowflake
    from discord.ext.commands._types import ContextT  # pyright: ignore[reportMissingTypeStubs]

    from utils.types import BlacklistBase

__all__ = ('Lagrange',)

log: logging.Logger = logging.getLogger(__name__)

jishaku.Flags.FORCE_PAGINATOR = True
jishaku.Flags.HIDE = True
jishaku.Flags.NO_DM_TRACEBACK = True
jishaku.Flags.USE_ANSI_ALWAYS = True
jishaku.Flags.NO_UNDERSCORE = True

EXTERNAL_COGS: list[str] = ['jishaku']


class Lagrange(commands.Bot):
    prefix: ClassVar[list[str]] = [
        ''.join(capitalization) for capitalization in product(*zip(BASE_PREFIX.lower(), BASE_PREFIX.upper(), strict=False))
    ]
    colour: discord.Colour = THEME_COLOUR
    session: aiohttp.ClientSession
    if TYPE_CHECKING:
        pool: asyncpg.Pool[asyncpg.Record]
    mystbin_cli: mystbin.Client
    load_time: datetime.datetime
    prefixes: dict[int, list[str]]
    blacklist: dict[Snowflake, BlacklistBase]
    maintenance: bool
    appinfo: discord.AppInfo
    invite_link: discord.Invite
    banner: discord.Asset

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        intents: discord.Intents = discord.Intents.all()
        self.token = BOT_TOKEN
        self.session = aiohttp.ClientSession()
        self.mystbin_cli = mystbin.Client()
        self.load_time = datetime.datetime.now()
        self.prefixes: dict[int, list[str]] = {}
        self.blacklist = {}
        self.maintenance = False
        super().__init__(
            description=DESCRIPTION,
            command_prefix=self.get_prefix,  # pyright: ignore[reportArgumentType]
            case_insensitive=True,
            strip_after_prefix=True,
            intents=intents,
            max_messages=5000,
            allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=True),
            *args,
            **kwargs,
        )

    @discord.utils.copy_doc(commands.Bot.get_prefix)
    async def get_prefix(self, message: discord.Message) -> list[str]:
        prefixes = self.prefix.copy()
        if message.guild is None:
            return commands.when_mentioned_or(*prefixes)(self, message)

        if self.prefixes.get(message.guild.id):
            prefixes.extend(self.prefixes[message.guild.id])
            return commands.when_mentioned_or(*prefixes)(self, message)

        fetched_prefix: list[str] = await self.pool.fetchval(
            """SELECT array_agg(prefix) FROM prefixes WHERE guild = $1""",
            (message.guild.id),
        )
        if fetched_prefix:
            self.prefixes[message.guild.id] = fetched_prefix
            prefixes.extend(self.prefixes[message.guild.id])

        return commands.when_mentioned_or(*prefixes)(self, message)

    async def add_prefix(self, guild: discord.Guild, prefix: str) -> list[str]:
        if prefix in self.prefix:
            raise PrefixAlreadyPresent(prefix)

        await self.pool.execute("""INSERT INTO Prefixes VALUES ($1, $2)""", guild.id, prefix)
        if not self.prefixes.get(guild.id):
            self.prefixes[guild.id] = [prefix]
            return self.prefixes[guild.id]
        self.prefixes[guild.id].append(prefix)

        return self.prefixes[guild.id]

    async def remove_prefix(self, guild: discord.Guild, prefix: str) -> list[str] | None:
        if not self.prefixes.get(guild.id):
            raise PrefixNotInitialised(guild)

        if prefix not in self.prefixes[guild.id]:
            raise PrefixNotPresent(prefix, guild)

        await self.pool.execute(
            """DELETE FROM Prefixes WHERE guild = $1 AND prefix = $2""",
            guild.id,
            prefix,
        )
        self.prefixes[guild.id].remove(prefix)
        if not self.prefixes[guild.id]:
            self.prefixes.pop(guild.id)
            return None
        return self.prefixes[guild.id]

    async def clear_prefix(self, guild: discord.Guild) -> None:
        if not self.prefixes.get(guild.id):
            raise PrefixNotInitialised(guild)

        await self.pool.execute("""DELETE FROM Prefixes WHERE guild = $1""", guild.id)

        self.prefixes.pop(guild.id)

    async def get_prefix_list(self, message: discord.Message) -> list[str]:
        prefixes = [BASE_PREFIX]
        if message.guild and self.prefixes.get(message.guild.id):
            prefixes.extend(self.prefixes[message.guild.id])

        return commands.when_mentioned_or(*prefixes)(self, message)

    async def check_blacklist(self, ctx: commands.Context[Self]) -> Literal[True]:
        if ctx.guild and self.is_blacklisted(ctx.guild):
            raise BlacklistedGuild(
                ctx.guild,
                reason=self.blacklist[ctx.guild]['reason'],
                until=self.blacklist[ctx.guild]['lasts_until'],
            )
        if ctx.author and self.is_blacklisted(ctx.author):
            raise BlacklistedUser(
                ctx.author,
                reason=self.blacklist[ctx.author]['reason'],
                until=self.blacklist[ctx.author]['lasts_until'],
            )

        return True

    def is_blacklisted(self, snowflake: discord.Member | discord.User | discord.Guild) -> bool:
        return bool(self.blacklist.get(snowflake))

    async def add_blacklist(
        self,
        snowflake: discord.User | discord.Guild,
        *,
        reason: str = 'No reason provided',
        lasts_until: datetime.datetime | None = None,
    ) -> dict[Snowflake, BlacklistBase]:
        if self.is_blacklisted(snowflake):
            raise AlreadyBlacklisted(
                snowflake,
                reason=self.blacklist[snowflake]['reason'],
                until=self.blacklist[snowflake]['lasts_until'],
            )

        sql = """INSERT INTO Blacklists (snowflake, reason, lasts_until, blacklist_type) VALUES ($1, $2, $3, $4);"""
        param = 'user' if isinstance(snowflake, discord.User) else 'guild'
        await self.pool.execute(
            sql,
            snowflake.id,
            reason,
            lasts_until,
            param,
        )
        self.blacklist[snowflake] = {'reason': reason, 'lasts_until': lasts_until, 'blacklist_type': param}
        return self.blacklist

    async def remove_blacklist(self, snowflake: discord.User | discord.Guild) -> dict[Snowflake, BlacklistBase]:
        if not self.is_blacklisted(snowflake):
            raise NotBlacklisted(snowflake)

        sql = """DELETE FROM Blacklists WHERE snowflake = $1"""
        param: str = 'user' if isinstance(snowflake, discord.User) else 'guild'
        await self.pool.execute(
            sql,
            snowflake.id,
            param,
        )

        self.blacklist.pop(snowflake)
        return self.blacklist

    async def check_maintenance(self, ctx: commands.Context[Self]) -> Literal[True]:
        if self.maintenance is True and not await self.is_owner(ctx.author):
            raise UnderMaintenance
        return True

    async def toggle_maintenance(self, toggle: bool | None = None) -> bool:
        if toggle:
            self.maintenance = toggle
            return self.maintenance
        self.maintenance = self.maintenance is False
        return self.maintenance

    async def setup_hook(self) -> None:
        credentials: dict[str, Any] = POSTGRES_CREDENTIALS
        pool: asyncpg.Pool[asyncpg.Record] | None = await asyncpg.create_pool(**credentials)
        if not pool or pool and pool._closed:
            msg = 'Pool is closed'
            raise RuntimeError(msg)
        self.pool = pool

        with Path('schema.sql').open(encoding='utf-8') as f:  # noqa: ASYNC230
            await self.pool.execute(f.read())

        self.appinfo = await self.application_info()
        self.invite_link = await self.fetch_invite(SERVER_INVITE)
        banner = (await self.fetch_user(self.user.id)).banner
        assert banner is not None
        self.banner = banner

        cogs = [m.name for m in iter_modules(['cogs'], prefix='cogs.')]
        cogs.extend(EXTERNAL_COGS)
        for cog in cogs:
            try:
                await self.load_extension(str(cog))
            except commands.ExtensionError as error:
                log.exception(
                    'Ignoring exception in loading %s',
                    cog,
                    exc_info=error,
                )
            else:
                log.info('Loaded %s ', cog)

        self.check_once(self.check_blacklist)
        self.check_once(self.check_maintenance)

    async def on_command_error(self, ctx: LagContext, exception: commands.CommandError) -> None:
        if isinstance(exception, BlacklistedUser | BlacklistedGuild):
            if isinstance(ctx.channel, discord.DMChannel):
                await ctx.reply(content=str(exception))
            elif ctx.guild and isinstance(exception, BlacklistedGuild):
                await ctx.guild.leave()
            return None
        elif isinstance(exception, UnderMaintenance | FeatureDisabled):
            await ctx.reply(str(exception))
            return None
        elif ctx.command and isinstance(exception, commands.CommandInvokeError):
            log.exception('Ignoring exception in command %s', ctx.command.name, exc_info=exception)

    @overload
    async def get_context(self, origin: discord.Interaction | discord.Message, /) -> LagContext: ...

    @overload
    async def get_context(self, origin: discord.Interaction | discord.Message, /, *, cls: type[ContextT]) -> ContextT: ...

    async def get_context(
        self,
        origin: discord.Interaction | discord.Message,
        /,
        *,
        cls: type[ContextT] = discord.utils.MISSING,
    ) -> ContextT:
        if cls is discord.utils.MISSING:
            cls = LagContext  # pyright: ignore[reportAssignmentType]
        return await super().get_context(origin, cls=cls)

    @discord.utils.copy_doc(commands.Bot.is_owner)
    async def is_owner(self, user: discord.abc.User) -> bool:
        return bool(user.id in OWNERS_ID)

    @functools.cached_property
    def logger_webhook(self) -> discord.Webhook:
        return discord.Webhook.from_url(WEBHOOK_URL, session=self.session, bot_token=self.token)

    @property
    def guild(self) -> discord.Guild:
        guild = self.get_guild(1262409199552430170)
        assert guild is not None
        return guild

    @property
    def user(self) -> discord.ClientUser:
        user = super().user
        assert user is not None
        return user

    async def close(self) -> None:
        if hasattr(self, 'pool'):
            await self.pool.close()
        if hasattr(self, 'session'):
            await self.session.close()
        await super().close()
