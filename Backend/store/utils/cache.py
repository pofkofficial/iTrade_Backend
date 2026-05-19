# utils/cache.py
from django.core.cache import cache

class CacheManager:
    @classmethod
    def get_phone_key(cls, phone_id, version=1):
        return f'phone:{phone_id}:v{version}'
    
    @classmethod
    def invalidate_phone(cls, phone_id):
        cache.delete(cls.get_phone_key(phone_id))
        cache.delete_pattern('phone_list:*')