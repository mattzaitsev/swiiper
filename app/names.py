"""Subreddit name rules shared by the API and the worker."""
import os
import re


def normalize_feed(value: str) -> str:
    value = value.strip().removeprefix('/r/').removeprefix('r/').rstrip('/').lower()
    if not re.fullmatch(r'[a-z0-9_]{1,50}', value):
        raise ValueError('Use one subreddit name containing letters, numbers or underscores')
    return value


# Reddit's RSS feeds don't say which communities are 18+, so this guesses from the name and
# leans toward flagging: a false positive only adds an age check. NSFW_SUBREDDITS and
# SFW_SUBREDDITS (comma-separated) correct it for specific communities.
NSFW_HINTS = ('nsfw', 'gonewild', 'porn', 'hentai', 'rule34', 'onlyfans', 'nude', 'xxx', 'incest',
              'femboy', 'milf', 'bdsm', 'fetish', 'lewd', 'slut', 'horny', 'boobs', 'titties',
              'pussy', 'cumshot', 'blowjob', 'yiff', 'futanari')
NSFW_NAMES = {'sex', 'ass', 'dick', 'cock', 'cum', 'tits', 'thick', 'pawg', 'realgirls', 'holdthemoan'}
NSFW_PREFIXES = ('sexy',)
NSFW_SUFFIXES = ('bbc',)
# Reddit's SFW "Porn" network: photography and design communities, not adult content.
SFW_NAMES = {name.lower() for name in (
    'EarthPorn', 'SkyPorn', 'spaceporn', 'WaterPorn', 'winterporn', 'AutumnPorn', 'BeachPorn',
    'DesertPorn', 'LakePorn', 'SeaPorn', 'SummitPorn', 'WeatherPorn', 'AuroraPorn', 'GeologyPorn',
    'BotanicalPorn', 'AnimalPorn', 'MacroPorn', 'MicroPorn', 'ExposurePorn', 'AerialPorn',
    'CityPorn', 'VillagePorn', 'RuralPorn', 'ArchitecturePorn', 'RoomPorn', 'CabinPorn',
    'CastlePorn', 'BridgePorn', 'ChurchPorn', 'CemeteryPorn', 'InfrastructurePorn',
    'AbandonedPorn', 'DestructionPorn', 'FirePorn', 'MachinePorn', 'MilitaryPorn', 'BoatPorn',
    'carporn', 'GunPorn', 'InstrumentPorn', 'DesignPorn', 'ArtPorn', 'StreetArtPorn',
    'BookPorn', 'MoviePosterPorn', 'HistoryPorn', 'MapPorn', 'FuturePorn', 'HumanPorn',
    'AdrenalinePorn', 'FoodPorn', 'DessertPorn', 'shittyfoodporn', 'powerwashingporn',
    'unixporn')}


def _listed(variable):
    return {name.strip().lower().removeprefix('r/') for name in os.getenv(variable, '').split(',')
            if name.strip()}


def is_nsfw(subreddit: str) -> bool:
    name = subreddit.lower()
    if name in _listed('NSFW_SUBREDDITS'):
        return True
    if name in _listed('SFW_SUBREDDITS') or name in SFW_NAMES:
        return False
    return (name in NSFW_NAMES or name.startswith(NSFW_PREFIXES) or name.endswith(NSFW_SUFFIXES)
            or any(hint in name for hint in NSFW_HINTS))
