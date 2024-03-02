import asyncio
import datetime as dt
import json
import traceback
from typing import Callable
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from babel.dates import format_datetime
from csxhair import Crosshair
from pyrogram import filters
from pyrogram.enums import ChatType, ChatAction, ParseMode
from pyrogram.errors import MessageDeleteForbidden, MessageNotModified
from pyrogram.types import CallbackQuery, Message
# noinspection PyUnresolvedReferences
from pyropatch import pyropatch  # do not delete!!
from telegraph.aio import Telegraph

from bottypes import BotClient, BotLogger, ExtendedIKB, ExtendedIKM, UserSession
import config
from db import db_session
from functions import datacenter_handlers, info_formatters, utime
from functions.decorators import ignore_message_not_modified
from functions.locale import get_available_languages, get_refined_lang_code
import keyboards
# noinspection PyPep8Naming
from l10n import Locale, LocaleKeys as LK
from utypes import (ExchangeRate, GameServers, GameVersion,
                    GunInfo, LeaderboardStats, ProfileInfo, State,
                    States, UserGameStats, drop_cap_reset_timer)
from utypes.profiles import ErrorCode, ParseUserStatsError  # to clearly indicate relation


GUNS_INFO = GunInfo.load()
AVAILABLE_LANGUAGES = get_available_languages()
ALL_COMMANDS = ('start', 'help')
ASK_TIMEOUT = 5 * 60

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(threadName)s: %(message)s",
                    datefmt="%H:%M:%S — %d/%m/%Y")

bot = BotClient(config.BOT_NAME,
                api_id=config.API_ID,
                api_hash=config.API_HASH,
                bot_token=config.BOT_TOKEN,
                plugins={'root': 'plugins'},
                test_mode=config.TEST_MODE,
                workdir=config.SESS_FOLDER,
                logger=BotLogger(config.LOGCHANNEL),
                navigate_back_callback=LK.bot_back,)

telegraph = Telegraph(access_token=config.TELEGRAPH_ACCESS_TOKEN)


# cat: Main

@bot.on_callback_exception()
async def handle_exceptions_in_callback(client: BotClient, session: UserSession, bot_message: Message, exc: Exception):
    logging.exception('Caught exception!', exc_info=exc)
    await client.log(f'❗️ {"".join(traceback.format_exception(exc))}',
                     disable_notification=True, parse_mode=ParseMode.DISABLED)

    return await something_went_wrong(client, session, bot_message)


@bot.on_message(~filters.me)
async def handle_messages(client: BotClient, message: Message):
    result = await client.handle_message(message)
    if result is None:
        message.continue_propagation()


@bot.on_callback_query()
async def handle_callbacks(client: BotClient, callback_query: CallbackQuery):
    if callback_query.message.chat.id != client.logger.log_channel_id:
        # Render selection indicator on selectable markups
        for markup in keyboards.all_selectable_markups:
            markup.select_button_by_key(callback_query.data)

    return await client.handle_callback(callback_query)


@bot.navmenu('main', ignore_message_not_modified=True)
@bot.navmenu(bot.WILDCARD, session_timeout=True)
async def main_menu(_, session: UserSession,
                    bot_message: Message, session_timeout: bool = False):
    text = session.locale.bot_choose_cmd

    if session_timeout:
        text = session.locale.error_session_timeout + '\n\n' + text
        if text == bot_message.text:  # rare edge case
            text += '‎'  # use empty char to bypass

    await bot_message.edit(text, reply_markup=keyboards.main_markup(session.locale))


# cat: Server stats


@bot.navmenu(LK.bot_servers_stats, came_from=main_menu, ignore_message_not_modified=True)
async def server_stats(_, session: UserSession, bot_message: Message):
    await bot_message.edit(session.locale.bot_choose_cmd,
                           reply_markup=keyboards.ss_markup(session.locale))


@bot.funcmenu(LK.game_status_button_title, came_from=server_stats, ignore_message_not_modified=True)
async def send_server_status(client: BotClient, session: UserSession, bot_message: Message):
    """Send the status of Counter-Strike servers"""

    data = GameServers.cached_server_status(config.CACHE_FILE_PATH)

    if data is States.UNKNOWN:
        return await something_went_wrong(client, session, bot_message)

    text = info_formatters.format_server_status(data, session.locale)

    await bot_message.edit(text, reply_markup=keyboards.ss_markup(session.locale))


@bot.funcmenu(LK.stats_matchmaking_button_title, came_from=server_stats, ignore_message_not_modified=True)
async def send_matchmaking_stats(client: BotClient, session: UserSession, bot_message: Message):
    """Send Counter-Strike matchamaking statistics"""

    data = GameServers.cached_matchmaking_stats(config.CACHE_FILE_PATH)

    if data is States.UNKNOWN:
        return await something_went_wrong(client, session, bot_message)

    text = info_formatters.format_matchmaking_stats(data, session.locale)

    await bot_message.edit(text, reply_markup=keyboards.ss_markup(session.locale))


# cat: Datacenters


@bot.navmenu(LK.dc_status_title, came_from=server_stats, ignore_message_not_modified=True)
async def datacenters(_, session: UserSession, bot_message: Message):
    await bot_message.edit(session.locale.dc_status_choose_region,
                           reply_markup=keyboards.dc_markup(session.locale))


@bot.funcmenu(LK.regions_africa, came_from=datacenters)
async def send_dc_africa(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.africa, keyboards.dc_markup)


@bot.funcmenu(LK.regions_australia, came_from=datacenters)
async def send_dc_australia(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.australia, keyboards.dc_markup)


@bot.navmenu(LK.regions_europe, came_from=datacenters, ignore_message_not_modified=True)
async def dc_europe(_, session: UserSession, bot_message: Message):
    await bot_message.edit(session.locale.dc_status_specify_country,
                           reply_markup=keyboards.dc_eu_markup(session.locale))


@bot.funcmenu(LK.dc_austria, came_from=dc_europe)
async def send_dc_austria(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.austria, keyboards.dc_eu_markup)


@bot.funcmenu(LK.dc_finland, came_from=dc_europe)
async def send_dc_finland(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.finland, keyboards.dc_eu_markup)


@bot.funcmenu(LK.dc_germany, came_from=dc_europe)
async def send_dc_germany(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.germany, keyboards.dc_eu_markup)


@bot.funcmenu(LK.dc_netherlands, came_from=dc_europe)
async def send_dc_netherlands(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.netherlands, keyboards.dc_eu_markup)


@bot.funcmenu(LK.dc_poland, came_from=dc_europe)
async def send_dc_poland(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.poland, keyboards.dc_eu_markup)


@bot.funcmenu(LK.dc_spain, came_from=dc_europe)
async def send_dc_spain(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.spain, keyboards.dc_eu_markup)


@bot.funcmenu(LK.dc_sweden, came_from=dc_europe)
async def send_dc_sweden(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.sweden, keyboards.dc_eu_markup)


@bot.navmenu(LK.dc_us, came_from=datacenters, ignore_message_not_modified=True)
async def dc_us(_, session: UserSession, bot_message: Message):
    await bot_message.edit(session.locale.dc_status_specify_region,
                           reply_markup=keyboards.dc_us_markup(session.locale))


@bot.funcmenu(LK.dc_us_east, came_from=dc_us)
async def send_dc_us_east(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.us_east, keyboards.dc_us_markup)


@bot.funcmenu(LK.dc_us_west, came_from=dc_us)
async def send_dc_us_west(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.us_west, keyboards.dc_us_markup)


@bot.navmenu(LK.regions_southamerica, came_from=datacenters, ignore_message_not_modified=True)
async def dc_southamerica(_, session: UserSession, bot_message: Message):
    await bot_message.edit(session.locale.dc_status_specify_country,
                           reply_markup=keyboards.dc_southamerica_markup(session.locale))


@bot.funcmenu(LK.dc_argentina, came_from=dc_southamerica)
async def send_dc_argentina(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message,
                        datacenter_handlers.argentina, keyboards.dc_southamerica_markup)


@bot.funcmenu(LK.dc_brazil, came_from=dc_southamerica)
async def send_dc_brazil(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message,
                        datacenter_handlers.brazil, keyboards.dc_southamerica_markup)


@bot.funcmenu(LK.dc_chile, came_from=dc_southamerica)
async def send_dc_chile(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message,
                        datacenter_handlers.chile, keyboards.dc_southamerica_markup)


@bot.funcmenu(LK.dc_peru, came_from=dc_southamerica)
async def send_dc_peru(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message,
                        datacenter_handlers.peru, keyboards.dc_southamerica_markup)


@bot.navmenu(LK.regions_asia, came_from=datacenters, ignore_message_not_modified=True)
async def dc_asia(_, session: UserSession, bot_message: Message):
    await bot_message.edit(session.locale.dc_status_specify_country,
                           reply_markup=keyboards.dc_asia_markup(session.locale))


@bot.funcmenu(LK.dc_india, came_from=dc_asia)
async def send_dc_india(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.india, keyboards.dc_asia_markup)


@bot.funcmenu(LK.dc_japan, came_from=dc_asia)
async def send_dc_japan(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.japan, keyboards.dc_asia_markup)


@bot.funcmenu(LK.regions_china, came_from=dc_asia)
async def send_dc_china(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.china, keyboards.dc_asia_markup)


@bot.funcmenu(LK.dc_emirates, came_from=dc_asia)
async def send_dc_emirates(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.emirates, keyboards.dc_asia_markup)


@bot.funcmenu(LK.dc_singapore, came_from=dc_asia)
async def send_dc_singapore(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.singapore, keyboards.dc_asia_markup)


@bot.funcmenu(LK.dc_hongkong, came_from=dc_asia)
async def send_dc_hongkong(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.hongkong, keyboards.dc_asia_markup)


@bot.funcmenu(LK.dc_southkorea, came_from=dc_asia)
async def send_dc_south_korea(client: BotClient, session: UserSession, bot_message: Message):
    await send_dc_state(client, session, bot_message, datacenter_handlers.south_korea, keyboards.dc_asia_markup)


@ignore_message_not_modified
async def send_dc_state(client: BotClient, session: UserSession, bot_message: Message,
                        dc_state_func: Callable[[Locale], str | State], reply_markup: ExtendedIKM):
    try:
        game_servers_datetime = GameServers.latest_info_update(config.CACHE_FILE_PATH)
        if game_servers_datetime is States.UNKNOWN:
            return await something_went_wrong(client, session, bot_message)

        state = dc_state_func(session.locale)
        lang_code = get_refined_lang_code(session.locale)
        state += '\n\n'
        state += f'{format_datetime(game_servers_datetime, "HH:mm:ss, dd MMM", locale=lang_code).title()} (UTC)'

        await bot_message.edit(state, reply_markup=reply_markup(session.locale))
    except Exception as e:
        return await handle_exceptions_in_callback(client, session, bot_message, e)

# cat: Profile info


@bot.navmenu(LK.bot_profile_info, came_from=main_menu, ignore_message_not_modified=True)
async def profile_info(client: BotClient, session: UserSession, bot_message: Message):
    with open(config.CACHE_FILE_PATH, encoding='utf-8') as f:
        cache_file = json.load(f)

    if cache_file.get('webapi') != 'normal':
        return await send_about_maintenance(client, session, bot_message)

    await bot_message.edit(session.locale.bot_choose_cmd,
                           reply_markup=keyboards.profile_markup(session.locale))


@bot.navmenu(LK.user_profileinfo_title, came_from=profile_info)
async def user_profile_info(client: BotClient, session: UserSession, bot_message: Message, last_error: str = None):
    text = session.locale.steam_url_example if last_error is None else last_error
    text += '\n\n' + session.locale.bot_use_cancel

    steam_url = await client.ask_message_silently(bot_message, text, timeout=ASK_TIMEOUT)

    return await user_profile_info_process(client, session, bot_message, steam_url)


@bot.message_process(of=user_profile_info)
async def user_profile_info_process(client: BotClient, session: UserSession, bot_message: Message, user_input: Message):
    if user_input.text == '/cancel':
        await user_input.delete()
        return await profile_info(client, session, bot_message)

    await bot_message.edit(session.locale.bot_loading)
    await client.send_chat_action(bot_message.chat.id, ChatAction.TYPING)

    try:
        info = await ProfileInfo.get(user_input.text)
    except ParseUserStatsError as e:
        await user_input.delete()
        error_msg = await user_info_handle_error(client, session, user_input, e)
        return await user_profile_info(client, session, bot_message, last_error=error_msg)
    except Exception as e:
        await user_input.delete()
        raise e

    if info.vanity_url is None:
        info.vanity_url = session.locale.user_profileinfo_notset

    if info.account_created:
        info.account_created = dt.datetime.fromtimestamp(info.account_created)
        info.account_created = f'{format_datetime(info.account_created, "dd MMM yyyy", locale=session.lang_code).title()}'
    else:
        info.account_created = session.locale.states_unknown

    info.faceit_ban = session.locale.user_profileinfo_banned \
        if info.faceit_ban else session.locale.user_profileinfo_none

    if info.faceit_lvl is None:
        info.faceit_lvl = session.locale.user_profileinfo_none
        info.faceit_elo = session.locale.user_profileinfo_none

    if info.faceit_url is None:
        info.faceit_url = session.locale.user_profileinfo_notfound

    if info.vac_bans == 0:
        info.vac_bans = session.locale.user_profileinfo_none

    if info.game_bans == 0:
        info.game_bans = session.locale.user_profileinfo_none

    info.community_ban = session.locale.user_profileinfo_banned \
        if info.community_ban else session.locale.user_profileinfo_none

    info.trade_ban = session.locale.user_profileinfo_banned \
        if info.trade_ban else session.locale.user_profileinfo_none

    text = session.locale.user_profileinfo_text.format(*info.to_tuple())

    await user_input.reply(text, disable_web_page_preview=True)
    return await user_input.reply(session.locale.bot_loading)


@bot.navmenu(LK.user_gamestats_button_title, came_from=profile_info, ignore_message_not_modified=True)
async def user_game_stats(client: BotClient, session: UserSession, bot_message: Message, last_error: str = None):
    text = session.locale.steam_url_example if last_error is None else last_error
    text += '\n\n' + session.locale.bot_use_cancel

    steam_url = await client.ask_message_silently(bot_message, text, timeout=ASK_TIMEOUT)

    return await user_game_stats_process(client, session, bot_message, steam_url)


@bot.message_process(of=user_game_stats)
async def user_game_stats_process(client: BotClient, session: UserSession, bot_message: Message, user_input: Message):
    if user_input.text == '/cancel':
        await user_input.delete()
        return await profile_info(client, session, bot_message)

    await bot_message.edit(session.locale.bot_loading)
    await client.send_chat_action(bot_message.chat.id, ChatAction.TYPING)

    try:
        user_stats = await UserGameStats.get(user_input.text)
    except ParseUserStatsError as e:
        await user_input.delete()
        error_msg = await user_info_handle_error(client, session, user_input, e)
        return await user_game_stats(client, session, bot_message, last_error=error_msg)
    except Exception as e:
        await user_input.delete()
        raise e

    steamid, *stats = user_stats
    stats_page_title = session.locale.user_gamestats_page_title.format(steamid)
    stats_page_text = info_formatters.format_user_game_stats(stats, session.locale)

    try:
        telegraph_response = await telegraph.create_page(stats_page_title,
                                                         html_content=stats_page_text,
                                                         author_name='@INCS2bot',
                                                         author_url='https://t.me/INCS2bot')
    except json.JSONDecodeError:
        await user_input.delete()
        return await user_game_stats(client, session, bot_message, last_error=session.locale.user_telegraph_error)

    share_btn = ExtendedIKB(session.locale.user_gamestats_share,
                            switch_inline_query=telegraph_response['url'])
    markup_share = ExtendedIKM([[share_btn]])

    await user_input.reply(telegraph_response['url'], reply_markup=markup_share)
    return await user_input.reply(session.locale.bot_loading)


async def user_info_handle_error(_, session: UserSession, user_input: Message, exc: ParseUserStatsError):
    if exc.is_unknown:
        await user_input.delete()
        raise exc

    error_msg = session.locale.user_invalidrequest_error
    if exc.code == ErrorCode.INVALID_LINK:
        error_msg = session.locale.user_invalidlink_error
    elif exc.code == ErrorCode.PROFILE_IS_PRIVATE:
        error_msg = '<a href="https://i.imgur.com/CAjblvT.mp4">‎</a>' + \
                    session.locale.user_privateprofile_error
    elif exc.code == ErrorCode.NO_STATS_AVAILABLE:
        error_msg = session.locale.user_nostatsavailable_error

    return error_msg


# cat: Extra features


@bot.navmenu(LK.bot_extras, came_from=main_menu, ignore_message_not_modified=True)
async def extra_features(_, session: UserSession, bot_message: Message):
    await bot_message.edit(session.locale.bot_choose_cmd,
                           reply_markup=keyboards.extra_markup(session.locale))


@bot.navmenu(LK.crosshair, came_from=extra_features, ignore_message_not_modified=True)
async def crosshair(_, session: UserSession, bot_message: Message):
    return await bot_message.edit(session.locale.bot_choose_func,
                                  reply_markup=keyboards.crosshair_markup(session.locale))  # must return


@bot.funcmenu(LK.crosshair_generate, came_from=crosshair, ignore_message_not_modified=True)
async def generate_crosshair(_, session: UserSession, bot_message: Message):  # todo: finally make this shit
    await bot_message.edit(session.locale.error_wip,
                           reply_markup=keyboards.crosshair_markup(session.locale))


@bot.navmenu(LK.crosshair_decode, came_from=crosshair, ignore_message_not_modified=True)
async def decode_crosshair(client: BotClient, session: UserSession,
                           bot_message: Message, last_error: str = None):
    text = session.locale.crosshair_decode_example if last_error is None else last_error
    text += '\n\n' + session.locale.bot_use_cancel

    decode_input = await client.ask_message_silently(bot_message, text, timeout=ASK_TIMEOUT)

    return await decode_crosshair_process(client, session, bot_message, decode_input)


@bot.message_process(of=decode_crosshair)
async def decode_crosshair_process(client: BotClient, session: UserSession, bot_message: Message, user_input: Message):
    if user_input.text == "/cancel":
        await user_input.delete()
        return await crosshair(client, session, bot_message)

    await bot_message.edit(session.locale.bot_loading)

    try:
        _crosshair = Crosshair.decode(user_input.text)
    except ValueError:
        await user_input.delete()
        return await decode_crosshair(client, session, bot_message, last_error=session.locale.crosshair_decode_error)

    text = session.locale.crosshair_decode_result.format('; '.join(_crosshair.cs2_commands))

    await user_input.reply(text)
    return await user_input.reply(session.locale.bot_loading)


@bot.funcmenu(LK.exchangerate_button_title, came_from=extra_features, ignore_message_not_modified=True)
async def send_exchange_rate(_, session: UserSession, bot_message: Message):
    prices = ExchangeRate.cached_data(config.CACHE_FILE_PATH).asdict()

    await bot_message.edit(session.locale.exchangerate_text.format(*prices.values()),
                           reply_markup=keyboards.extra_markup(session.locale))


@bot.funcmenu(LK.valve_hqtime_button_title, came_from=extra_features, ignore_message_not_modified=True)
async def send_valve_hq_time(_, session: UserSession, bot_message: Message):
    """Send the time in Valve headquarters (Bellevue, Washington, US)"""

    text = info_formatters.format_valve_hq_time(session.locale)

    await bot_message.edit(text, reply_markup=keyboards.extra_markup(session.locale))


@bot.funcmenu(LK.game_dropcap_button_title, came_from=extra_features, ignore_message_not_modified=True)
async def send_dropcap_timer(_, session: UserSession, bot_message: Message):
    """Send drop cap reset time"""

    text = session.locale.game_dropcaptimer_text.format(*drop_cap_reset_timer())

    await bot_message.edit(text, reply_markup=keyboards.extra_markup(session.locale))


@bot.funcmenu(LK.game_version_button_title, came_from=extra_features, ignore_message_not_modified=True)
async def send_game_version(_, session: UserSession, bot_message: Message):
    """Send a current version of CS:GO/CS 2"""

    data = GameVersion.cached_data(config.CACHE_FILE_PATH)
    text = info_formatters.format_game_version_info(data, session.locale)

    await bot_message.edit(text, reply_markup=keyboards.extra_markup(session.locale),
                           disable_web_page_preview=True)


@bot.navmenu(LK.game_leaderboard_button_title, came_from=extra_features, ignore_message_not_modified=True)
async def game_leaderboard(_, session: UserSession, bot_message: Message):
    world_data = LeaderboardStats.cached_world_stats(config.CACHE_FILE_PATH)
    text = info_formatters.format_game_world_leaderboard(world_data, session.locale)

    await bot_message.edit(text, reply_markup=keyboards.leaderboard_markup(session.locale))


@bot.funcmenu(LK.game_leaderboard_world, came_from=game_leaderboard)
async def game_leaderboard_world(client: BotClient, session: UserSession, bot_message: Message):
    return await send_game_leaderboard(client, session, bot_message, LK.game_leaderboard_world)


@bot.funcmenu(LK.regions_africa, came_from=game_leaderboard)
async def game_leaderboard_africa(client: BotClient, session: UserSession, bot_message: Message):
    return await send_game_leaderboard(client, session, bot_message, LK.regions_africa)


@bot.funcmenu(LK.regions_asia, came_from=game_leaderboard)
async def game_leaderboard_asia(client: BotClient, session: UserSession, bot_message: Message):
    return await send_game_leaderboard(client, session, bot_message, LK.regions_asia)


@bot.funcmenu(LK.regions_australia, came_from=game_leaderboard)
async def game_leaderboard_australia(client: BotClient, session: UserSession, bot_message: Message):
    return await send_game_leaderboard(client, session, bot_message, LK.regions_australia)


@bot.funcmenu(LK.regions_china, came_from=game_leaderboard)
async def game_leaderboard_china(client: BotClient, session: UserSession, bot_message: Message):
    return await send_game_leaderboard(client, session, bot_message, LK.regions_china)


@bot.funcmenu(LK.regions_europe, came_from=game_leaderboard)
async def game_leaderboard_europe(client: BotClient, session: UserSession, bot_message: Message):
    return await send_game_leaderboard(client, session, bot_message, LK.regions_europe)


@bot.funcmenu(LK.regions_northamerica, came_from=game_leaderboard)
async def game_leaderboard_northamerica(client: BotClient, session: UserSession, bot_message: Message):
    return await send_game_leaderboard(client, session, bot_message, LK.regions_northamerica)


@bot.funcmenu(LK.regions_southamerica, came_from=game_leaderboard)
async def game_leaderboard_southamerica(client: BotClient, session: UserSession, bot_message: Message):
    return await send_game_leaderboard(client, session, bot_message, LK.regions_southamerica)


@bot.navmenu(LK.game_leaderboard_button_title, came_from=game_leaderboard, ignore_message_not_modified=True)
async def send_game_leaderboard(_, session: UserSession, bot_message: Message,
                                region: str = LK.game_leaderboard_world):
    """Sends the CS2 leaderboard (top-10), supports both world and regional"""

    keyboards.leaderboard_markup.select_button_by_key(region)

    await bot_message.edit(session.locale.bot_loading,
                           reply_markup=keyboards.leaderboard_markup(session.locale))

    region = region.split('_')[-1]
    if region == 'world':
        data = LeaderboardStats.cached_world_stats(config.CACHE_FILE_PATH)
        text = info_formatters.format_game_world_leaderboard(data, session.locale)
    else:
        data = LeaderboardStats.cached_regional_stats(config.CACHE_FILE_PATH, region)
        text = info_formatters.format_game_regional_leaderboard(region, data, session.locale)

    await bot_message.edit(text, reply_markup=keyboards.leaderboard_markup(session.locale))


# cat: Crosshair editor

async def edit_crosshair_style(client: BotClient, session: UserSession, bot_message: Message, data: dict = None):
    chosen_style = await client.ask_callback_silently(bot_message,
                                                      'Choose a style for your crosshair',
                                                      reply_markup=ExtendedIKM([
                                                          [ExtendedIKB('Classic', 'classic')],
                                                          [ExtendedIKB('Classic Static', 'classic_static')],
                                                          [ExtendedIKB('Traditional', 'traditional')],
                                                          [keyboards.back_button],
                                                      ]),
                                                      timeout=ASK_TIMEOUT)


# @bot.callback_process(of=edit_crosshair_style)
async def edit_crosshair_style_process(client: BotClient, session: UserSession, callback_query: CallbackQuery):
    await client.log_callback(session, callback_query)

    styles = {'classic': 0, 'classic_static': 0, 'traditional': 0}  # todo: what are the real values

    chosen_style = callback_query.data
    bot_message = callback_query.message

    if chosen_style in styles:
        keyboards.pistols_markup.select_button_by_key(chosen_style)
        return await send_gun_info(client, session, bot_message, pistols, styles[chosen_style],
                                   reply_markup=keyboards.pistols_markup)
    if chosen_style == LK.bot_back:
        return await client.go_back(session, bot_message)
    return await unknown_request(client, session, bot_message, keyboards.pistols_markup)

# cat: Guns info


@bot.navmenu(LK.gun_button_text, came_from=extra_features)
async def guns(_, session: UserSession, bot_message: Message):
    await bot_message.edit(session.locale.gun_select_category,
                           reply_markup=keyboards.guns_markup(session.locale))


@bot.navmenu(LK.gun_pistols, came_from=guns)
async def pistols(client: BotClient, session: UserSession, bot_message: Message, loop: bool = False):
    if loop:
        chosen_gun = await client.listen_callback(bot_message.chat.id,
                                                  bot_message.id,
                                                  timeout=ASK_TIMEOUT)
    else:
        chosen_gun = await client.ask_callback_silently(bot_message,
                                                        session.locale.gun_select_pistol,
                                                        reply_markup=keyboards.pistols_markup(session.locale),
                                                        timeout=ASK_TIMEOUT)

    return await pistols_process(client, session, chosen_gun)


@bot.callback_process(of=pistols)
async def pistols_process(client: BotClient, session: UserSession, callback_query: CallbackQuery):
    await client.log_callback(session, callback_query)

    chosen_gun = callback_query.data
    bot_message = callback_query.message

    if chosen_gun in GUNS_INFO:
        keyboards.pistols_markup.select_button_by_key(chosen_gun)
        return await send_gun_info(client, session, bot_message, pistols, GUNS_INFO[chosen_gun],
                                   reply_markup=keyboards.pistols_markup)
    if chosen_gun == LK.bot_back:
        return await client.go_back(session, bot_message)
    return await unknown_request(client, session, bot_message, keyboards.pistols_markup)


@bot.navmenu(LK.gun_heavy, came_from=guns)
async def heavy(client: BotClient, session: UserSession, bot_message: Message, loop: bool = False):
    if loop:
        chosen_gun = await client.listen_callback(bot_message.chat.id,
                                                  bot_message.id,
                                                  timeout=ASK_TIMEOUT)
    else:
        chosen_gun = await client.ask_callback_silently(bot_message,
                                                        session.locale.gun_select_heavy,
                                                        reply_markup=keyboards.heavy_markup(session.locale),
                                                        timeout=ASK_TIMEOUT)

    return await heavy_process(client, session, chosen_gun)


@bot.callback_process(of=heavy)
async def heavy_process(client: BotClient, session: UserSession, callback_query: CallbackQuery):
    await client.log_callback(session, callback_query)

    chosen_gun = callback_query.data
    bot_message = callback_query.message

    if chosen_gun in GUNS_INFO:
        keyboards.heavy_markup.select_button_by_key(chosen_gun)
        return await send_gun_info(client, session, bot_message, heavy, GUNS_INFO[chosen_gun],
                                   reply_markup=keyboards.heavy_markup)
    if chosen_gun == LK.bot_back:
        return await client.go_back(session, bot_message)
    return await unknown_request(client, session, bot_message, keyboards.heavy_markup)


@bot.navmenu(LK.gun_smgs, came_from=guns)
async def smgs(client: BotClient, session: UserSession, bot_message: Message, loop: bool = False):
    if loop:
        chosen_gun = await client.listen_callback(bot_message.chat.id,
                                                  bot_message.id,
                                                  timeout=ASK_TIMEOUT)
    else:
        chosen_gun = await client.ask_callback_silently(bot_message,
                                                        session.locale.gun_select_smg,
                                                        reply_markup=keyboards.smgs_markup(session.locale),
                                                        timeout=ASK_TIMEOUT)

    return await smgs_process(client, session, chosen_gun)


@bot.callback_process(of=smgs)
async def smgs_process(client: BotClient, session: UserSession, callback_query: CallbackQuery):
    await client.log_callback(session, callback_query)

    chosen_gun = callback_query.data
    bot_message = callback_query.message

    if chosen_gun in GUNS_INFO:
        keyboards.smgs_markup.select_button_by_key(chosen_gun)
        return await send_gun_info(client, session, bot_message, smgs, GUNS_INFO[chosen_gun],
                                   reply_markup=keyboards.smgs_markup)
    if chosen_gun == LK.bot_back:
        return await client.go_back(session, bot_message)
    return await unknown_request(client, session, bot_message, keyboards.smgs_markup)


@bot.navmenu(LK.gun_rifles, came_from=guns)
async def rifles(client: BotClient, session: UserSession, bot_message: Message, loop: bool = False):
    if loop:
        chosen_gun = await client.listen_callback(bot_message.chat.id,
                                                  bot_message.id,
                                                  timeout=ASK_TIMEOUT)
    else:
        chosen_gun = await client.ask_callback_silently(bot_message,
                                                        session.locale.gun_select_rifle,
                                                        reply_markup=keyboards.rifles_markup(session.locale),
                                                        timeout=ASK_TIMEOUT)

    return await rifles_process(client, session, chosen_gun)


@bot.callback_process(of=rifles)
async def rifles_process(client: BotClient, session: UserSession, callback_query: CallbackQuery):
    await client.log_callback(session, callback_query)

    chosen_gun = callback_query.data
    bot_message = callback_query.message

    if chosen_gun in GUNS_INFO:
        keyboards.rifles_markup.select_button_by_key(chosen_gun)
        return await send_gun_info(client, session, bot_message, rifles, GUNS_INFO[chosen_gun],
                                   reply_markup=keyboards.rifles_markup)
    if chosen_gun == LK.bot_back:
        return await client.go_back(session, bot_message)
    return await unknown_request(client, session, bot_message, keyboards.rifles_markup)


async def send_gun_info(client: BotClient, session: UserSession, bot_message: Message, _from: Callable,
                        gun_info: GunInfo, reply_markup: ExtendedIKM):
    """Send archived data about guns"""

    try:
        gun_info_dict = gun_info.as_dict()
        gun_info_dict['origin'] = session.locale.get(gun_info.origin)
        del gun_info_dict['id'], gun_info_dict['team']

        text = session.locale.gun_summary_text.format(*gun_info_dict.values())

        try:
            await bot_message.edit(text, reply_markup=reply_markup(session.locale))
        except MessageNotModified:
            pass
        finally:
            return await _from(client, session, bot_message, loop=True)
    except Exception as e:
        return await handle_exceptions_in_callback(client, session, bot_message, e)


# cat: Settings


@bot.navmenu(LK.bot_settings, came_from=main_menu, ignore_message_not_modified=True)
async def settings(_, session: UserSession, bot_message: Message):
    await bot_message.edit(session.locale.bot_choose_setting,
                           reply_markup=keyboards.settings_markup(session.locale))


@bot.navmenu(LK.settings_language_button_title, came_from=settings, ignore_message_not_modified=True)
async def language(client: BotClient, session: UserSession, bot_message: Message):
    keyboards.language_settings_markup.select_button_by_key(session.locale.lang_code)

    chosen_lang = await client.ask_callback_silently(
        bot_message,
        session.locale.settings_language_choose.format(AVAILABLE_LANGUAGES.get(session.locale.lang_code)),
        reply_markup=keyboards.language_settings_markup(session.locale),
        timeout=ASK_TIMEOUT
    )

    return await language_process(client, session, chosen_lang)


@bot.callback_process(of=language)
async def language_process(client: BotClient, session: UserSession, callback_query: CallbackQuery):
    await client.log_callback(session, callback_query)

    chosen_lang = callback_query.data
    bot_message = callback_query.message

    if chosen_lang == LK.bot_back:
        return await client.go_back(session, bot_message)
    if chosen_lang in AVAILABLE_LANGUAGES:
        session.update_lang(chosen_lang)
    return await language(client, session, bot_message)


# cat: Commands


@bot.on_command('start')
async def welcome(client: BotClient, session: UserSession, message: Message):
    """First bot's message"""

    if message.chat.type != ChatType.PRIVATE:
        return await pm_only(client, session, message)

    text = session.locale.bot_start_text.format(message.from_user.first_name)
    await message.reply(text)

    session.current_menu_id = main_menu.id
    return await message.reply(session.locale.bot_choose_cmd, reply_markup=keyboards.main_markup(session.locale))


@bot.on_command('feedback')
async def leave_feedback(client: BotClient, session: UserSession, message: Message):
    """Send feedback"""

    if message.chat.type != ChatType.PRIVATE:
        return await pm_only(client, session, message)

    await message.reply('You can report any bugs found in the bot '
                        'or submit a suggestion on our '
                        '[GitHub page](https://github.com/aquaismissing/INCS2bot/issues).')

    session.current_menu_id = main_menu.id
    return await message.reply(session.locale.bot_choose_cmd, reply_markup=keyboards.main_markup(session.locale))


@bot.on_command('help')
async def _help(client: BotClient, session: UserSession, message: Message):
    """/help message"""

    if message.chat.type != ChatType.PRIVATE:
        return await pm_only(client, session, message)

    await message.reply(session.locale.bot_help_text)

    session.current_menu_id = main_menu.id
    return await message.reply(session.locale.bot_choose_cmd, reply_markup=keyboards.main_markup(session.locale))


# cat: Service


async def pm_only(_, session: UserSession, message: Message):
    msg = await message.reply(session.locale.bot_pmonly_text)

    try:
        await asyncio.sleep(10)
        await message.delete()
    except MessageDeleteForbidden:
        pass
    finally:
        await msg.delete()


@ignore_message_not_modified
async def send_about_maintenance(_, session: UserSession, bot_message: Message):
    session.current_menu_id = main_menu.id
    await bot_message.edit(session.locale.valve_steam_maintenance_text,
                           reply_markup=keyboards.main_markup(session.locale))


@ignore_message_not_modified
async def something_went_wrong(_, session: UserSession, bot_message: Message):
    """If anything goes wrong"""

    session.current_menu_id = main_menu.id
    await bot_message.edit(session.locale.error_internal,
                           reply_markup=keyboards.main_markup(session.locale))


@ignore_message_not_modified
async def unknown_request(_, session: UserSession, bot_message: Message,
                          reply_markup: ExtendedIKM = keyboards.main_markup):
    await bot_message.edit(session.locale.error_unknownrequest,
                           reply_markup=reply_markup(session.locale))


async def regular_stats_report(client: BotClient):
    now = utime.utcnow()

    text = (f'📊 **Some stats for the past 8 hours:**\n'
            f'\n'
            f'• Unique users served: {len(client.rstats.unique_users_served)}\n'
            f'• Callback queries handled: {client.rstats.callback_queries_handled}\n'
            f'• Inline queries handled: {client.rstats.inline_queries_handled}\n'
            f'• Exceptions caught: {client.rstats.exceptions_caught}\n'
            f'\n'
            f'📁 **Other stats:**\n'
            f'\n'
            f'• Bot started up at: {client.startup_dt:%Y-%m-%d %H:%M:%S} (UTC)\n'
            f'• Is working for: {info_formatters.format_timedelta(now - client.startup_dt)}')
    await client.log(text, instant=True)
    client.rstats.clear()


async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(bot.clear_timeout_sessions, 'interval', minutes=30)
    scheduler.add_job(regular_stats_report, 'interval', hours=8,
                      args=(bot,))

    scheduler.start()

    try:
        await db_session.init(config.USER_DB_FILE_PATH)
        await bot.start()
        await bot.log('Bot started.', instant=True)
        await bot.mainloop()
    except Exception as e:
        logging.exception('The bot got terminated because of exception!')
        await bot.log(f'Bot got terminated because of exception!\n'
                      f'\n'
                      f'❗️ {e.__traceback__}', disable_notification=False)
    finally:
        logging.info('Shutting down the bot...')
        await bot.log('Bot is shutting down...', instant=True)
        await bot.dump_sessions()
        await bot.stop(block=False)


if __name__ == '__main__':
    bot.run(main())
