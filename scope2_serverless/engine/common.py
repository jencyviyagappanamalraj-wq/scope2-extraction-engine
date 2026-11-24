"""Small helpers shared by the API and worker Lambdas."""
import ipaddress
import json
import os
import socket
import time
from decimal import Decimal
from urllib.parse import urlparse


def now():
    return int(time.time())


def check_url(url):
    """Basic SSRF protection: only http(s) and never private/internal addresses."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("url must start with http:// or https://")
    for info in socket.getaddrinfo(p.hostname, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("url points to a private or internal address")
    return url


def to_ddb(obj):
    """DynamoDB does not accept Python floats."""
    return json.loads(json.dumps(obj), parse_float=Decimal)


class Enc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        return super().default(o)


def cost_usd(tokens_in, tokens_out):
    """AI cost of one job. Check the current Bedrock price of your model and set the two variables."""
    pin = float(os.environ.get("PRICE_IN_PER_1K", "0.003"))
    pout = float(os.environ.get("PRICE_OUT_PER_1K", "0.015"))
    return round(tokens_in / 1000 * pin + tokens_out / 1000 * pout, 5)
