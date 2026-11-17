"""Recovery classification; persistent request/token budgets remain authoritative."""
import random


def transient_failure(message):
    if 'provider_cooldown:' in message:return False
    return any(code in message for code in (
        'provider_http_408', 'provider_http_429', 'provider_http_500',
        'provider_http_502', 'provider_http_503', 'provider_http_504',
        'ReadTimeout', 'ConnectTimeout', 'ConnectionError', 'ProxyError'))


def retry_delay(attempt, base=10, retry_after=0):
    return min(60, max(float(retry_after), base * 2 ** attempt + random.uniform(0, 3)))
