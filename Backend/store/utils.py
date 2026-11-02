# store/utils.py
import hashlib
import hmac

from django.conf import settings


def get_client_ip(request):
    """Real client IP. On Vercel the platform sets the first X-Forwarded-For entry.
    Locally there's no header, so it falls back to REMOTE_ADDR."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def hash_ip(ip):
    """Salted hash (64 hex chars). Unlike plain SHA-256, it can't be reversed
    by brute-forcing the IPv4 address space without your SECRET_KEY."""
    return hmac.new(settings.SECRET_KEY.encode(), ip.encode(), hashlib.sha256).hexdigest()