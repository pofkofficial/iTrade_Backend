# middleware.py
from django.utils.deprecation import MiddlewareMixin
from .models import Visit
from django.contrib.auth.models import AnonymousUser
import logging

logger = logging.getLogger(__name__)

class CacheHitMissMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        
    def __call__(self, request):
        response = self.get_response(request)
        
        if hasattr(request, 'cache_hit'):
            response['X-Cache'] = 'HIT' if request.cache_hit else 'MISS'
            
        return response
    
class VisitTrackingMiddleware(MiddlewareMixin):
    """Track website visits, excluding admin, static, media, and bot requests."""
    
    def process_request(self, request):
        """Log visit details to the Visit model."""
        # Skip admin, static, media routes
        if request.path.startswith(('/admin/', '/static/', '/media/')):
            return None

        # Skip bot requests
        user_agent = request.META.get('HTTP_USER_AGENT', '')
        if Visit.is_bot_user_agent(user_agent):
            return None

        try:
            # Safely access user and session
            user = getattr(request, 'user', None)
            user = user if user and not isinstance(user, AnonymousUser) else None
            session_id = getattr(request, 'session', None) and request.session.session_key or None

            Visit.objects.create(
                ip_address=request.META.get('REMOTE_ADDR', ''),
                user_agent=user_agent,
                referrer=request.META.get('HTTP_REFERER', ''),
                session_id=session_id,
                user=user,
                path=request.path
            )
        except Exception as e:
            logger.error(f"Visit tracking error for path {request.path}: {str(e)}", exc_info=True)

        return None