# store/throttles.py
from rest_framework.throttling import AnonRateThrottle


class LoginThrottle(AnonRateThrottle):
    scope = 'login'


class TrackThrottle(AnonRateThrottle):
    scope = 'track'


class CheckoutThrottle(AnonRateThrottle):
    scope = 'checkout'


class ContactThrottle(AnonRateThrottle):
    scope = 'contact'