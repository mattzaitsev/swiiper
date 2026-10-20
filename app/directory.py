"""Local subreddit directory for search suggestions: loaded communities plus a curated list.

No third-party directory is called. Member counts aren't known locally, so none are shown.
"""
from .names import is_nsfw

# Popular, media-friendly communities, in display casing. Matching is case-insensitive.
POPULAR = (
    'pics', 'aww', 'gaming', 'EarthPorn', 'oddlysatisfying', 'space', 'funny', 'memes', 'videos',
    'gifs', 'interestingasfuck', 'nextfuckinglevel', 'mildlyinteresting', 'Damnthatsinteresting',
    'BeAmazed', 'NatureIsFuckingLit', 'AnimalsBeingDerps', 'AnimalsBeingBros', 'AnimalsBeingJerks',
    'rarepuppers', 'cats', 'dogs', 'dogpictures', 'catpictures', 'Eyebleach', 'StartledCats', 'Blep',
    'corgi', 'goldenretrievers', 'husky', 'Rabbits', 'Otters', 'redpandas', 'sneks', 'birding',
    'wildlifephotography', 'FoodPorn', 'food', 'Cooking', 'Baking', 'Breadit', 'Pizza', 'Coffee',
    'cocktails', 'CityPorn', 'VillagePorn', 'ArchitecturePorn', 'RoomPorn', 'CabinPorn',
    'AbandonedPorn', 'spaceporn', 'SkyPorn', 'WaterPorn', 'winterporn', 'HistoryPorn', 'MapPorn',
    'landscapephotography', 'photography', 'itookapicture', 'analog', 'astrophotography',
    'wallpaper', 'wallpapers', 'Art', 'CozyPlaces', 'houseplants', 'gardening', 'succulents',
    'Bonsai', 'woodworking', 'DIY', 'crafts', 'knitting', 'crochet', 'Pottery', '3Dprinting',
    'travel', 'hiking', 'camping', 'climbing', 'surfing', 'skateboarding', 'snowboarding', 'cars',
    'carporn', 'motorcycles', 'battlestations', 'pcmasterrace', 'buildapc', 'MechanicalKeyboards',
    'unixporn', 'lego', 'Minecraft', 'leagueoflegends', 'GlobalOffensive', 'apexlegends',
    'FortNiteBR', 'pokemon', 'nintendo', 'NintendoSwitch', 'PS5', 'xbox', 'Steam', 'Eldenring',
    'skyrim', 'pcgaming', 'Games', 'sports', 'nba', 'nfl', 'soccer', 'formula1', 'hockey',
    'baseball', 'MMA', 'Fitness', 'running', 'bicycling', 'movies', 'television', 'marvelstudios',
    'StarWars', 'anime', 'manga', 'comics', 'books', 'Music', 'Guitar', 'science', 'Physics',
    'dataisbeautiful', 'Futurology', 'technology', 'gadgets', 'ProgrammerHumor', 'wholesomememes',
    'MadeMeSmile', 'HumansBeingBros', 'powerwashingporn',
)
_POPULAR_RANK = {name.lower(): rank for rank, name in enumerate(POPULAR)}


def suggest(query: str, loaded: dict[str, int], include_nsfw: bool = False, limit: int = 12):
    """Rank communities whose name contains `query`.

    `loaded` maps each subreddit already in the database to its post count. Exact and prefix
    matches come first, then loaded communities (most posts first), then popular ones.
    """
    display = {name.lower(): name for name in POPULAR}
    names = {name for name in loaded if query in name} | {name for name in display if query in name}
    rows = []
    for name in names:
        nsfw = is_nsfw(name)
        if nsfw and not include_nsfw:
            continue
        rows.append({'subreddit': name, 'displayName': display.get(name, name),
                     'posts': loaded.get(name), 'popular': name in display, 'nsfw': nsfw})
    rows.sort(key=lambda row: (row['subreddit'] != query, not row['subreddit'].startswith(query),
                               row['posts'] is None, -(row['posts'] or 0),
                               _POPULAR_RANK.get(row['subreddit'], len(POPULAR)), row['subreddit']))
    return rows[:limit]
