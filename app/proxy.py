"""Oxylabs proxy for the worker, used when no Web API key is set (see app/webapi.py).

Products and entry points (Oxylabs docs, "Quick Start: Proxies"):

    residential, mobile    pr.oxylabs.io:7777    customer-USER[-cc-US][-st-us_california][-sessid-ID[-sesstime-MIN]]
    datacenter (shared)    dc.oxylabs.io:8000    user-USER[-country-US]
    isp (shared)           isp.oxylabs.io:8000   user-USER[-country-US]
    dedicated-datacenter   ddc.oxylabs.io:8000   user-USER   (or the per-IP port from the dashboard proxy list)
    dedicated-isp          disp.oxylabs.io:8000  user-USER

Credentials are the *proxy user* created in the Oxylabs dashboard. They are not the Web API key.
A sticky session id is chosen once per worker start. It is never changed in response to a denial.
"""
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

PRODUCTS = {
    'residential': ('pr.oxylabs.io', 7777, 'customer'),
    'mobile': ('pr.oxylabs.io', 7777, 'customer'),
    'datacenter': ('dc.oxylabs.io', 8000, 'user'),
    'isp': ('isp.oxylabs.io', 8000, 'user'),
    'dedicated-datacenter': ('ddc.oxylabs.io', 8000, 'user'),
    'dedicated-isp': ('disp.oxylabs.io', 8000, 'user'),
}
IP_CHECK_URLS = [('http_gateway', 'http://ip.oxylabs.io/location'),
                 ('https_connect', 'https://ip.oxylabs.io/location')]


@dataclass(frozen=True)
class ProxyEntry:
    url: str                                    # http://host:port, never with embedded credentials
    headers: dict = field(default_factory=dict)  # {'Proxy-Authorization': 'Basic …'}
    label: str = ''                             # safe for logs: no password, username masked

    def __repr__(self):                         # keep credentials out of tracebacks and logs
        return f'ProxyEntry({self.label})'


def _setting(name, default=''):
    return os.getenv(name, default).strip()


def _check(name, value, pattern, hint):
    if value and not re.fullmatch(pattern, value):
        raise ValueError(f'{name} {hint}')
    return value


def oxylabs_username(product: str, username: str, country: str = '', state: str = '',
                     session_id: str = '', session_minutes: int = 0) -> str:
    """Build the documented username for a product. Accepts the dashboard user with or without its prefix."""
    _, _, prefix = PRODUCTS[product]
    base = re.sub(r'^(customer|user)-', '', username)
    parts = [f'{prefix}-{base}']
    if prefix == 'customer':
        if country:
            parts.append(f'cc-{country.upper()}')
        if state:
            parts.append(f'st-{state.lower()}')
        if session_id:
            parts.append(f'sessid-{session_id}')
            if session_minutes:
                parts.append(f'sesstime-{session_minutes}')
    elif country and not product.startswith('dedicated'):
        parts.append(f'country-{country.upper()}')
    return '-'.join(parts)


def _credentials():
    """Username and password from the Docker secret file if it has them, else from the environment."""
    path = _setting('OXYLABS_PROXY_CREDENTIALS_FILE')
    if path and Path(path).is_file():
        for number, line in enumerate(Path(path).read_text(encoding='utf-8-sig').splitlines(), 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            username, sep, password = line.partition(':')
            if not sep or not username or not password:
                raise ValueError(f'Invalid Oxylabs credentials file on line {number}: use username:password')
            return username.strip(), password
    return _setting('OXYLABS_PROXY_USERNAME'), os.getenv('OXYLABS_PROXY_PASSWORD', '')


def load_proxy() -> ProxyEntry:
    product = _setting('OXYLABS_PROXY_PRODUCT', 'residential').lower()
    if product not in PRODUCTS:
        raise ValueError(f'OXYLABS_PROXY_PRODUCT must be one of: {", ".join(PRODUCTS)}')
    host, port, _ = PRODUCTS[product]

    endpoint = _check('OXYLABS_PROXY_ENDPOINT', _setting('OXYLABS_PROXY_ENDPOINT'),
                      r'[A-Za-z0-9.-]+:\d{1,5}', 'must be host:port, e.g. ddc.oxylabs.io:8001')
    if endpoint:
        host, port = endpoint.rsplit(':', 1)[0], int(endpoint.rsplit(':', 1)[1])
        if not 1 <= port <= 65535:
            raise ValueError('OXYLABS_PROXY_ENDPOINT port must be 1-65535')

    country = _check('OXYLABS_PROXY_COUNTRY', _setting('OXYLABS_PROXY_COUNTRY'),
                     r'[A-Za-z]{2}', 'must be a 2-letter country code, e.g. US')
    state = _check('OXYLABS_PROXY_STATE', _setting('OXYLABS_PROXY_STATE'),
                   r'us_[a-z_]+', 'must look like us_california (US states only)')
    if state and (product not in ('residential', 'mobile') or country.upper() != 'US'):
        raise ValueError('OXYLABS_PROXY_STATE needs a residential or mobile product and OXYLABS_PROXY_COUNTRY=US')
    try:
        minutes = int(_setting('OXYLABS_PROXY_SESSION_MINUTES', '0') or 0)
    except ValueError:
        raise ValueError('OXYLABS_PROXY_SESSION_MINUTES must be a whole number') from None
    if not 0 <= minutes <= 1440:
        raise ValueError('OXYLABS_PROXY_SESSION_MINUTES must be 0 (new IP per request) to 1440')

    username, password = _credentials()
    if not username or not password:
        raise ValueError('Set OXYLABS_WEB_API_KEY (your Oxylabs Web API key), or OXYLABS_PROXY_USERNAME '
                         'and OXYLABS_PROXY_PASSWORD for an Oxylabs proxy user')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', username):
        raise ValueError('OXYLABS_PROXY_USERNAME may contain only letters, digits, _ . and -')

    session_id = secrets.token_hex(6) if minutes and product in ('residential', 'mobile') else ''
    login = oxylabs_username(product, username, country, state, session_id, minutes)
    masked = login.replace(re.sub(r'^(customer|user)-', '', username), '***', 1)
    return ProxyEntry(url=f'http://{host}:{port}',
                      headers={'Proxy-Authorization': aiohttp.encode_basic_auth(login, password)},
                      label=f'oxylabs {product} {host}:{port} as {masked}')
