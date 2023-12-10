from __future__ import annotations

import asyncio
import datetime as dt
from typing import Type

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified, UserIsBlocked
from pyrogram.types import (CallbackQuery, InlineQuery, Message,
                            MessageEntity, InlineKeyboardMarkup,
                            ReplyKeyboardMarkup,
                            ReplyKeyboardRemove, ForceReply, User)
# noinspection PyUnresolvedReferences
from pyropatch import pyropatch  # do not delete!!

from .extended_ik import ExtendedIKM
from .menu import Menu, NavMenu, FuncMenu
from .sessions import UserSession, UserSessions
from .stats import BotRegularStats

__all__ = ('BotClient',)


class BotClient(Client):
    """
    Custom pyrogram.Client class to add custom properties and methods and stop PyCharm annoy me.
    """

    LOGS_TIMEOUT = dt.timedelta(seconds=4)  # define how often logs should be sent

    WILDCARD = '_'

    def __init__(self, *args, log_channel_id: int, navigate_back_callback: str, **kwargs):
        super().__init__(*args, **kwargs)

        self.log_channel_id = log_channel_id
        self.navigate_back_callback = navigate_back_callback

        self._sessions: UserSessions = UserSessions()

        self._commands: dict[str, tuple | dict] = {}

        # menus
        self._menus: dict[str, Menu] = {}
        self._menu_routes: dict[str, Menu | dict[str, Menu]] = {}

        self.latest_log_dt = dt.datetime.now()

        # injects
        self._func_at_exception: callable = None

        self.startup_dt = None

        self.rstats = BotRegularStats()

    @property
    def sessions(self) -> UserSessions:
        return self._sessions

    @property
    def can_log_now(self) -> bool:
        return (dt.datetime.now() - self.latest_log_dt) >= self.LOGS_TIMEOUT

    @property
    def time_for_next_log(self) -> dt.timedelta:
        return self.LOGS_TIMEOUT - (dt.datetime.now() - self.latest_log_dt)

    async def start(self):
        await super().start()
        self.startup_dt = dt.datetime.now(dt.UTC)

    async def register_session(self, user: User, message: Message = None, *, force_lang: str = None) -> UserSession:
        session = await self._sessions.register_session(user, message, force_lang=force_lang)
        self.rstats.unique_users_served(user.id)
        return session

    async def dump_sessions(self):
        await self.clear_timeout_sessions()
        await self._sessions.sync_with_db()

    async def clear_timeout_sessions(self):
        """Clear all sessions that exceed a given lifetime."""

        return await self._sessions.clear_timeout_sessions()

    def clear_sessions(self):
        self._sessions.clear()

    def on_callback_exception(self):
        def decorator(func):
            self._func_at_exception = func
            return func

        return decorator

    def on_command(self, command: str, *args, **kwargs):
        def decorator(func):
            self._commands[command] = (func, args, kwargs)
            return func

        return decorator

    def _menu_factory(self,
                      _type: Type[Menu],
                      query: str,
                      *args,
                      _id: str,
                      came_from: Menu,  # menu where we clicked on button
                      ignore_message_not_modified: bool,
                      **kwargs):
        def decorator(func: callable | Menu):
            nonlocal _id

            if isinstance(func, Menu):
                menu = _type(func.id, func.func, *args,
                             came_from_menu_id=func.came_from_menu_id,
                             ignore_message_not_modified=func.ignore_message_not_modified,
                             **kwargs)
            else:
                if _id is None:
                    _id = func.__qualname__

                menu = _type(_id, func, *args, ignore_message_not_modified=ignore_message_not_modified, **kwargs)
                if came_from is not None:
                    menu.came_from_menu_id = came_from.id

            if menu not in self._menus.values():
                self.register_menu(menu)

            if came_from is None:
                self._menu_routes[query] = menu
                return menu

            if self._menu_routes.get(query):
                self._menu_routes[query][came_from.id] = menu
            else:
                self._menu_routes[query] = {came_from.id: menu}
            return menu

        return decorator

    def navmenu(self,
                query: str,
                *args,
                _id: str = None,
                came_from: callable | Menu = None,
                ignore_message_not_modified: bool = False,
                **kwargs):
        return self._menu_factory(NavMenu,
                                  query,
                                  *args,
                                  _id=_id,
                                  came_from=came_from,
                                  ignore_message_not_modified=ignore_message_not_modified,
                                  **kwargs)

    def funcmenu(self,
                 query: str,
                 *args,
                 _id: str = None,
                 came_from: callable | Menu = None,
                 ignore_message_not_modified: bool = False,
                 **kwargs):
        return self._menu_factory(FuncMenu,
                                  query,
                                  *args,
                                  _id=_id,
                                  came_from=came_from,
                                  ignore_message_not_modified=ignore_message_not_modified,
                                  **kwargs)

    @staticmethod
    def callback_process(of: callable | NavMenu):
        def decorator(func: callable):
            if not isinstance(of, NavMenu):
                raise TypeError('process can be set only to navmenu u doofus')

            of.callback_process = func
            return func

        return decorator

    def get_wildcard_command(self):
        return self._commands.get(self.WILDCARD)

    async def get_func_by_command(self, session: UserSession, message: Message):
        try:
            text = message.text.split('@', 2)[0]  # removing mention from command
            text = text.lstrip('/')
            func, args, kwargs = self._commands[text]
        except KeyError:
            if self.WILDCARD not in self._commands:
                return
            func, args, kwargs = self.get_wildcard_command()
        return await func(self, session, message, *args, **kwargs)

    def get_wildcard_menu(self):
        return self._menu_routes.get(self.WILDCARD)

    async def get_menu_by_callback(self, session: UserSession, callback_query: CallbackQuery):
        if callback_query.data == self.navigate_back_callback:
            return await self.go_back(session, callback_query)

        is_wildcard_menu = False
        try:
            possible_menus = self._menu_routes[callback_query.data]
            if isinstance(possible_menus, Menu):
                menu = possible_menus
            else:
                menu = possible_menus[session.current_menu_id]
        except KeyError:
            current_menu = self.get_menu(session.current_menu_id)
            if current_menu.has_callback_process():
                return await current_menu.callback_process(self, session, callback_query)

            if self.WILDCARD not in self._menu_routes:
                return
            menu = self.get_wildcard_menu()
            is_wildcard_menu = True

        self.rstats.callback_queries_handled()

        try:
            if is_wildcard_menu:
                return await self.jump_to_menu(session, callback_query, menu)
            return await self.go_to_menu(session, callback_query, menu)
        except MessageNotModified:
            if not menu.ignore_message_not_modified:
                raise
        except UserIsBlocked:
            pass
        except Exception as e:
            self.rstats.exceptions_caught()
            self._func_at_exception(self, session, callback_query, e)

    def register_menu(self, menu: Menu):
        if menu not in self._menus.values():
            self._menus[menu.id] = menu

    def get_menu(self, _id: str):
        return self._menus.get(_id)

    async def go_to_menu(self, session: UserSession, callback_query: CallbackQuery, menu: Menu):
        """
        Sends user to a specific menu if we can access that menu from the current one.

        Note:
            You can use ``BotClient.jump_to_menu(session, callback_query, menu)`` to avoid access check.
        """

        if not menu.can_come_from(session.current_menu_id):
            raise AttributeError(f'Can\'t access {menu} from {self.get_menu(session.current_menu_id)}')

        return await self.jump_to_menu(session, callback_query, menu)

    async def jump_to_menu(self, session: UserSession, callback_query: CallbackQuery, menu: Menu):
        """Sends user to a specific menu."""

        if isinstance(menu, NavMenu):
            session.previous_menu_id = menu.came_from_menu_id
            session.current_menu_id = menu.id

        return await menu(self, session, callback_query)

    async def go_back(self, session: UserSession, callback_query: CallbackQuery):
        if session.previous_menu_id is None:
            if self.WILDCARD not in self._menu_routes:
                return
            func = self.get_wildcard_menu()
            return await func(self, session, callback_query)

        previous_menu = self._menus[session.previous_menu_id]
        return await self.jump_to_menu(session, callback_query, previous_menu)

    # noinspection PyUnresolvedReferences
    async def listen_message(self,
                             chat_id: int,
                             filters=None,
                             timeout: int = None) -> Message:
        return await super().listen_message(chat_id, filters, timeout)

    # noinspection PyUnresolvedReferences
    async def ask_message(self,
                          chat_id: int,
                          text: str,
                          filters=None,
                          timeout: int = None,
                          parse_mode: ParseMode = None,
                          entities: list[MessageEntity] = None,
                          disable_web_page_preview: bool = None,
                          disable_notification: bool = None,
                          reply_to_message_id: int = None,
                          schedule_date: dt.datetime = None,
                          protect_content: bool = None,
                          reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup |
                          ReplyKeyboardRemove | ForceReply = None) -> Message:
        return await super().ask_message(chat_id,
                                         text,
                                         filters,
                                         timeout,
                                         parse_mode,
                                         entities,
                                         disable_web_page_preview,
                                         disable_notification,
                                         reply_to_message_id,
                                         schedule_date,
                                         protect_content,
                                         reply_markup)

    # noinspection PyUnresolvedReferences
    async def listen_callback(self,
                              chat_id: int = None,
                              message_id: int = None,
                              inline_message_id: str = None,
                              filters=None,
                              timeout: int = None) -> CallbackQuery:
        return await super().listen_callback(chat_id,
                                             message_id,
                                             inline_message_id,
                                             filters,
                                             timeout)

    async def ask_message_silently(self, callback_query: CallbackQuery,
                                   text: str, *args, reply_markup: ExtendedIKM = None, **kwargs) -> Message:
        """Asks for a message in the same message."""

        await callback_query.edit_message_text(text, *args, reply_markup=reply_markup, **kwargs)
        return await self.listen_message(callback_query.message.chat.id)

    async def ask_callback_silently(self, callback_query: CallbackQuery,
                                    text: str, *args, reply_markup: ExtendedIKM = None, **kwargs) -> CallbackQuery:
        """Asks for a callback query in the same message."""

        await callback_query.edit_message_text(text, *args, reply_markup=reply_markup, **kwargs)
        return await self.listen_callback(callback_query.message.chat.id, callback_query.message.id)

    async def log(self, text: str, *, disable_notification: bool = True,
                  reply_markup: ReplyKeyboardMarkup = None, parse_mode: ParseMode = None):
        """Sends log to the log channel."""

        if self.test_mode:
            return

        asyncio.create_task(self._log(text, disable_notification, reply_markup, parse_mode))

    async def log_message(self, session: UserSession, message: Message):
        """Sends message log to the log channel."""

        if self.test_mode:
            return

        asyncio.create_task(self._log_message(session, message))

    async def log_callback(self, session: UserSession, callback_query: CallbackQuery):
        """Sends callback log to the log channel."""

        if self.test_mode:
            return

        asyncio.create_task(self._log_callback(session, callback_query))

    async def log_inline(self, session: UserSession, inline_query: InlineQuery):
        """Sends an inline query to the log channel."""

        if self.test_mode:
            return

        asyncio.create_task(self._log_inline(session, inline_query))

    async def _log(self, text: str,
                   disable_notification: bool,
                   reply_markup: ReplyKeyboardMarkup,
                   parse_mode: ParseMode):
        if self.test_mode:
            return

        if not self.can_log_now:
            self.latest_log_dt = dt.datetime.now()                       # to ensure that we won't get two logs
            await asyncio.sleep(self.time_for_next_log.total_seconds())  # at the same time and fail order
        self.latest_log_dt = dt.datetime.now()

        await self.send_message(self.log_channel_id, text,
                                disable_notification=disable_notification,
                                reply_markup=reply_markup,
                                parse_mode=parse_mode)

    async def _log_message(self, session: UserSession, message: Message):
        if self.test_mode:
            return

        if not self.can_log_now:
            self.latest_log_dt = dt.datetime.now()                       # to ensure that we won't get two logs
            await asyncio.sleep(self.time_for_next_log.total_seconds())  # at the same time and fail order
        self.latest_log_dt = dt.datetime.now()

        username = message.from_user.username
        display_name = f'@{username}' if username is not None else f'{message.from_user.mention} (username hidden)'

        text = (f'✍️ User: {display_name}\n'
                f'ID: {message.from_user.id}\n'
                f'Telegram language: {message.from_user.language_code}\n'
                f'Chosen language: {session.locale.lang_code}\n'
                f'Private message: {message.text!r}')
        await self.send_message(self.log_channel_id, text, disable_notification=True)

    async def _log_callback(self, session: UserSession, callback_query: CallbackQuery):
        if self.test_mode:
            return

        if not self.can_log_now:
            self.latest_log_dt = dt.datetime.now()                       # to ensure that we won't get two logs
            await asyncio.sleep(self.time_for_next_log.total_seconds())  # at the same time and fail order
        self.latest_log_dt = dt.datetime.now()

        username = callback_query.from_user.username
        display_name = f'@{username}' if username is not None \
            else f'{callback_query.from_user.mention} (username hidden)'

        text = (f'🔀 User: {display_name}\n'
                f'ID: {callback_query.from_user.id}\n'
                f'Telegram language: {callback_query.from_user.language_code}\n'
                f'Chosen language: {session.locale.lang_code}\n'
                f'Callback query: {callback_query.data}')
        await self.send_message(self.log_channel_id, text, disable_notification=True)

    async def _log_inline(self, session: UserSession, inline_query: InlineQuery):
        if self.test_mode:
            return

        if not self.can_log_now:
            self.latest_log_dt = dt.datetime.now()                       # to ensure that we won't get two logs
            await asyncio.sleep(self.time_for_next_log.total_seconds())  # at the same time and fail order
        self.latest_log_dt = dt.datetime.now()

        username = inline_query.from_user.username
        display_name = f'@{username}' if username is not None else f'{inline_query.from_user.mention} (username hidden)'

        text = (f'🛰 User: {display_name}\n'
                f'ID: {inline_query.from_user.id}\n'
                f'Telegram language: {inline_query.from_user.language_code}\n'
                f'Chosen language: {session.locale.lang_code}\n'
                f'Inline query: {inline_query.query!r}')
        await self.send_message(self.log_channel_id, text, disable_notification=True)
