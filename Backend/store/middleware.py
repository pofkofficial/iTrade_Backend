# store/middleware.py
import logging

from django.core.cache import cache
from django.http import HttpResponseNotFound

from .models import SecurityEvent
from .utils import get_client_ip, hash_ip

logger = logging.getLogger(__name__)

PROBE_PREFIXES = ('/wp-', '/wordpress', '/phpmyadmin', '/xmlrpc', '/cgi-bin', '/actuator', '/server-status')
BURST_THRESHOLD = 5      # probes from one IP before a SecurityEvent is logged
BURST_WINDOW = 60 * 60   # seconds


class CacheHitMissMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if hasattr(request, 'cache_hit'):
            response['X-Cache'] = 'HIT' if request.cache_hit else 'MISS'
        return response


class ProbeShieldMiddleware:
    """Reject scanner traffic (/.env, /.aws/credentials, /wp-admin, ...)
    before sessions, auth or any view runs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._is_probe(request.path.lower()):
            self._record(request)
            return HttpResponseNotFound()
        return self.get_response(request)

    @staticmethod
    def _is_probe(path):
        # Any path segment starting with a dot (/.env, /api/.git/config),
        # except /.well-known/, which legitimate services use.
        if '/.' in path and '/.well-known/' not in path:
            return True
        return path.startswith(PROBE_PREFIXES)

    @staticmethod
    def _record(request):
        # Bookkeeping must never break a request, so every failure is swallowed.
        try:
            ip_hash = hash_ip(get_client_ip(request))
            key = f'probe:{ip_hash}'
            cache.add(key, 0, BURST_WINDOW)
            if cache.incr(key) == BURST_THRESHOLD:   # fires once per burst
                SecurityEvent.objects.create(
                    kind='probe',
                    ip_hash=ip_hash,
                    path=request.path[:200],
                    severity='low',
                )
        except Exception:
            logger.warning('Probe shield bookkeeping failed', exc_info=True)