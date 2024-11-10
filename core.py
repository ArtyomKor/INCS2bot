import asyncio
import datetime as dt
import platform

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pandas as pd
from pyrogram import Client
if platform.system() == 'Linux':
    # noinspection PyPackageRequirements
    import uvloop

    uvloop.install()

import config
from dcatlas import DatacenterAtlas
from functions import caching, utime
from functions.ulogging import get_logger
from l10n import locale
from utypes import (ExchangeRate, Datacenter,
                    DatacenterRegion, DatacenterGroup, GameServers,
                    LeaderboardStats, State, SteamWebAPI,
                    LEADERBOARD_API_REGIONS)


DATACENTER_API_FIELDS = {
    'south_africa': {'johannesburg': 'South Africa'},
    'australia': {'sydney': 'Australia'},
    'sweden': {'stockholm': 'EU Sweden'},
    'germany': {'frankfurt': 'EU Germany'},
    'finland': {'helsinki': 'EU Finland'},
    'spain': {'madrid': 'EU Spain'},
    'netherlands': {'amsterdam': 'EU Holland'},  # unknown
    'austria': {'vienna': 'EU Austria'},
    'poland': {'warsaw': 'EU Poland'},
    'us_east': {'chicago': 'US Chicago',
                'sterling': 'US Virginia',
                'new_york': 'US NewYork',  # unknown
                'atlanta': 'US Atlanta'},
    'us_west': {'seattle': 'US Seattle',
                'los_angeles': 'US California'},
    'brazil': {'sao_paulo': 'Brazil'},
    'chile': {'santiago': 'Chile'},
    'peru': {'lima': 'Peru'},
    'argentina': {'buenos_aires': 'Argentina'},
    'hongkong': 'Hong Kong',
    'india': {'mumbai': 'India Mumbai',
              'chennai': 'India Chennai',
              'bombay': 'India Bombay',  # unknown
              'madras': 'India Madras'},  # unknown
    'uk': {'london': 'United Kingdom'},
    'china': {'shanghai': 'China Shanghai',  # unknown
              'tianjin': 'China Tianjin',
              'guangzhou': 'China Guangzhou',
              'chengdu': 'China Chengdu',
              'pudong': 'China Pudong'},
    'south_korea': {'seoul': 'South Korea'},
    'singapore': 'Singapore',
    'emirates': {'dubai': 'Emirates'},
    'japan': {'tokyo': 'Japan'},
}


UNKNOWN_DC_STATE = {"capacity": "unknown", "load": "unknown"}


execution_start_dt = dt.datetime.now()
execution_cron = (execution_start_dt + dt.timedelta(minutes=2)).replace(second=0)

update_cache_interval = 40
unique_monthly_timing = 0
check_currency_timing = 15
fetch_leaderboard_timing = 30

loc = locale('ru')

logger = get_logger('core', config.LOGS_FOLDER, config.LOGS_CONFIG_FILE_PATH)

scheduler = AsyncIOScheduler()
bot = Client(config.BOT_CORE_MODULE_NAME,
             api_id=config.API_ID,
             api_hash=config.API_HASH,
             bot_token=config.BOT_TOKEN,
             test_mode=config.TEST_MODE,
             no_updates=True,
             workdir=config.SESS_FOLDER)
steam_webapi = SteamWebAPI(config.STEAM_API_KEY, headers=config.REQUESTS_HEADERS)


def remap_dc(info: dict, fields: dict[str, str], dc: Datacenter):
    api_info_field = fields[dc.id]
    return info.get(api_info_field, UNKNOWN_DC_STATE)


def remap_dc_region(info: dict, fields: dict[str, dict[str, str]], region: DatacenterRegion):
    region_fields = fields[region.id]
    return {dc.id: remap_dc(info, region_fields, dc) for dc in region.datacenters}


def remap_dc_group(info: dict, fields: dict[str, dict[str, dict[str, str]]], group: DatacenterGroup):
    group_fields = fields[group.id]
    return {region.id: remap_dc_region(info, group_fields, region) for region in group.regions}


def remap_datacenters_info(info: dict) -> dict:
    dcs = DatacenterAtlas.available_dcs()
    
    remapped_info = {}
    for dc in dcs:
        match dc:
            case Datacenter(id=i):
                remapped_info[i] = remap_dc(info, DATACENTER_API_FIELDS, dc)
            case DatacenterRegion(id=i):
                remapped_info[i] = remap_dc_region(info, DATACENTER_API_FIELDS, dc)
            case DatacenterGroup(id=i):
                remapped_info[i] = remap_dc_group(info, DATACENTER_API_FIELDS, dc)
            case _:
                logger.warning(f'Found non-datacenter object in DatacenterAtlas: {dc}')

    return remapped_info


@scheduler.scheduled_job('interval', seconds=update_cache_interval)
async def update_cache_info():
    # noinspection PyBroadException
    try:
        cache = caching.load_cache(config.CORE_CACHE_FILE_PATH)

        game_servers_data = GameServers.request(steam_webapi)

        for key, value in game_servers_data.asdict().items():
            if key == 'datacenters':
                continue
            if isinstance(value, State):
                value = value.literal
            cache[key] = value

        cache['datacenters'] = remap_datacenters_info(game_servers_data.datacenters)

        if cache.get('online_players', 0) > cache.get('player_alltime_peak', 1):
            if scheduler.get_job('players_peak') is None:
                # to collect new peak for 15 minutes and then post the highest one
                scheduler.add_job(alert_players_peak, id='players_peak',
                                  next_run_time=dt.datetime.now() + dt.timedelta(minutes=15), coalesce=True)
            cache['player_alltime_peak'] = cache['online_players']

        df = pd.read_csv(config.PLAYER_CHART_FILE_PATH, parse_dates=['DateTime'])
        now = utime.utcnow()
        end_date = f'{now:%Y-%m-%d %H:%M:%S}'
        start_date = f'{(now - dt.timedelta(days=1)):%Y-%m-%d %H:%M:%S}'
        mask = (df['DateTime'] > start_date) & (df['DateTime'] <= end_date)
        player_24h_peak = int(df.loc[mask]['Players'].max())

        cache['player_24h_peak'] = player_24h_peak

        caching.dump_cache(config.CORE_CACHE_FILE_PATH, cache)
    except Exception:
        logger.exception('Caught exception while updating the cache!')

        
@scheduler.scheduled_job('cron',
                         hour=execution_cron.hour, minute=execution_cron.minute, second=unique_monthly_timing)
async def unique_monthly():
    # noinspection PyBroadException
    try:
        new_player_count = steam_webapi.csgo_get_monthly_player_count()

        cache = caching.load_cache(config.CORE_CACHE_FILE_PATH)

        if cache.get('monthly_unique_players') is None:
            cache['monthly_unique_players'] = new_player_count

        if new_player_count != cache['monthly_unique_players']:
            await send_alert('monthly_unique_players',
                             (cache['monthly_unique_players'], new_player_count))
            cache['monthly_unique_players'] = new_player_count

        caching.dump_cache(config.CORE_CACHE_FILE_PATH, cache)
    except Exception:
        logger.exception('Caught exception while gathering monthly players!')
        await asyncio.sleep(45)
        return await unique_monthly()


@scheduler.scheduled_job('cron',
                         hour=execution_cron.hour, minute=execution_cron.minute, second=check_currency_timing)
async def check_currency():
    # noinspection PyBroadException
    try:
        new_prices = ExchangeRate.request(steam_webapi).asdict()

        caching.dump_cache_changes(config.CORE_CACHE_FILE_PATH, {'key_price': new_prices})
    except Exception:
        logger.exception('Caught exception while gathering key price!')
        await asyncio.sleep(45)
        return await check_currency()


@scheduler.scheduled_job('cron',
                         hour=execution_cron.hour, minute=execution_cron.minute, second=fetch_leaderboard_timing)
async def fetch_leaderboard():
    # noinspection PyBroadException
    try:
        world_leaderboard_stats = LeaderboardStats.request_world(steam_webapi.session)
        new_data = {'world_leaderboard_stats': world_leaderboard_stats}

        for region in LEADERBOARD_API_REGIONS:
            regional_leaderboard_stats = LeaderboardStats.request_regional(steam_webapi.session, region)
            new_data[f'regional_leaderboard_stats_{region}'] = regional_leaderboard_stats

        caching.dump_cache_changes(config.CORE_CACHE_FILE_PATH, new_data)
    except Exception:
        logger.exception('Caught exception fetching leaderboards!')
        await asyncio.sleep(45)
        return await fetch_leaderboard()


async def alert_players_peak():
    # noinspection PyBroadException
    try:
        cache = caching.load_cache(config.CORE_CACHE_FILE_PATH)

        await send_alert('online_players', cache['player_alltime_peak'])
    except Exception:
        logger.exception('Caught exception while alerting players peak!')
        await asyncio.sleep(45)
        return await alert_players_peak()


async def send_alert(key, new_value):
    if key == 'online_players':
        text = loc.notifs_new_playerspeak.format(new_value)
    elif key == 'monthly_unique_players':
        text = loc.notifs_new_monthlyunique.format(*new_value)
    else:
        logger.warning(f'Got wrong key to send alert: {key}')
        return

    if bot.test_mode:
        chat_list = [config.AQ]
    else:
        chat_list = [config.INCS2CHAT, config.CSTRACKER]

    for chat_id in chat_list:
        msg = await bot.send_message(chat_id, text)
        if chat_id == config.INCS2CHAT:
            await msg.pin(disable_notification=True)


def main():
    logger.info('Started.')
    try:
        scheduler.start()
        bot.run()
    except TypeError:  # catching TypeError because Pyrogram propagates it at stop for some reason
        logger.info('Shutting down the bot...')
    finally:
        steam_webapi.close()
        logger.info('Terminated.')


if __name__ == '__main__':
    main()
