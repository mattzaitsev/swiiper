"""Oxylabs Web API: one Bearer key, and Oxylabs fetches the page (POST /v1/scrape).

This is the default way the worker reaches Reddit. It needs only the key from the dashboard's
Web API instance; an Oxylabs proxy user (app/proxy.py) is the alternative for proxy plans.
"""
import os
import re
from dataclasses import dataclass, field

WEB_API_URL = 'https://webapi.oxylabs.io/v1/scrape'


@dataclass(frozen=True)
class WebApi:
    key: str = field(repr=False)
    endpoint: str = WEB_API_URL
    label: str = 'oxylabs web api'

    def __repr__(self):
        return f'WebApi({self.label})'


def load_web_api() -> WebApi | None:
    """The Web API when OXYLABS_WEB_API_KEY is set, else None."""
    key = os.getenv('OXYLABS_WEB_API_KEY', '').strip()
    if not key:
        return None
    if not re.fullmatch(r'[A-Za-z0-9._~+/=-]{16,512}', key):
        # Never echo the value: it is a credential even when malformed.
        raise ValueError('OXYLABS_WEB_API_KEY does not look like a Web API key')
    return WebApi(key)
