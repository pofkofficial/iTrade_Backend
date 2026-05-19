# store/views.py
from django.conf import settings
from django.contrib.auth import login, logout
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.db import transaction
from django.db.models import Prefetch, Count, Q
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers
from django.core.cache import cache
from rest_framework import generics, status, permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from rest_framework.authentication import TokenAuthentication, SessionAuthentication
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from .models import Visit, ContactMessage, Phone, Order, StorageVariant, RAMVariant, OrderItem
from .serializers import (
    PhoneListSerializer, PhoneDetailSerializer,
    OrderWriteSerializer, OrderReadSerializer, OrderUpdateSerializer,
    ContactMessageSerializer, ContactMessageCreateSerializer, ContactMessageStatusSerializer, PhoneUpdateSerializer, StorageVariantDetailSerializer,
    StorageVariantListSerializer, RAMVariantListSerializer, RAMVariantDetailSerializer, StorageVariantWriteSerializer,
    UserLoginSerializer
)

import json
import logging
import requests
from datetime import timedelta
import time

# Configuration
CACHE_TTL = getattr(settings, 'CACHE_TTL', 60 * 15)  # 15-minute cache timeout
logger = logging.getLogger(__name__)


# ============================================================================
# CUSTOM PAGINATION CLASSES
# ============================================================================

class PhoneListPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class OrderListPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 50


# ============================================================================
# AUTHENTICATION VIEWS
# ============================================================================

class LoginView(APIView):
    """Handles token-based authentication for admin users."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        """Authenticate user and return token and user details."""
        logger.info(f"Login attempt from IP: {request.META.get('REMOTE_ADDR')}")
        
        serializer = UserLoginSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {'status': 'error', 'message': serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = serializer.validated_data['user']
        
        try:
            token, _ = Token.objects.get_or_create(user=user)
            if 'sessionid' not in request.COOKIES:
                login(request, user)
            
            logger.info(f"Successful login for superuser: {user.username}")
            return Response({
                'status': 'success',
                'token': token.key,
                'user': {
                    'id': user.id,
                    'username': user.username,
                    'email': user.email,
                    'is_superuser': user.is_superuser
                }
            })
        except Exception as e:
            logger.error(f"Login error: {str(e)}")
            return Response(
                {'status': 'error', 'message': 'Authentication failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class LogoutView(APIView):
    """Handles user logout by deleting token and clearing session."""
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Delete user token and session."""
        try:
            request.user.auth_token.delete()
            if hasattr(request, 'session'):
                logout(request)
            return Response(
                {'status': 'success', 'message': 'Successfully logged out'},
                status=status.HTTP_200_OK
            )
        except Exception as e:
            logger.error(f"Logout error: {str(e)}")
            return Response(
                {'status': 'error', 'message': 'Logout failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class CurrentUserView(APIView):
    """Returns details of the authenticated user."""
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Retrieve current user details."""
        user = request.user
        return Response({
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'is_superuser': user.is_superuser
        })


# ============================================================================
# PHONE VIEWS (Optimized)
# ============================================================================

class PhoneList(generics.ListCreateAPIView):
    """List and create phones with optimized caching and pagination."""
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    pagination_class = None
    CACHE_VERSION_KEY = 'phone_list_cache_version'

    def get_serializer_class(self):
        if self.request.method == 'GET':
            return PhoneListSerializer
        return PhoneDetailSerializer

    @method_decorator(cache_page(CACHE_TTL))
    @method_decorator(vary_on_headers('Authorization', 'Cookie'))
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def get_cache_key(self):
        version = cache.get(self.CACHE_VERSION_KEY, 1)
        params = self.request.query_params.urlencode()
        return f'phone_list_optimized:v{version}:{params}'

    def get_queryset(self):
        """Ultra-fast queryset with minimal field selection."""
        cache_key = self.get_cache_key()
        cached_data = cache.get(cache_key)
        
        if cached_data is not None:
            return cached_data

        # Minimal field selection for maximum performance
        queryset = Phone.objects.only(
            'id', 'brand', 'model', 'thumbnail'
        ).order_by('id')

        # Apply filters if provided
        brand = self.request.query_params.get('brand')
        if brand:
            queryset = queryset.filter(brand__iexact=brand)

        cache.set(cache_key, queryset, timeout=CACHE_TTL)
        return queryset

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def perform_create(self, serializer):
        instance = serializer.save()
        try:
            cache.incr(self.CACHE_VERSION_KEY)
        except ValueError:
            cache.set(self.CACHE_VERSION_KEY, 2)
        cache.set(f'{self.CACHE_VERSION_KEY}_invalidated_at', time.time())

class FeaturedList(generics.ListAPIView):
    """
    Smart Featured Phones Endpoint

    GET /api/featured/                  → Top 5 most ordered phones (all brands)
    GET /api/featured/?brand=samsung     → Top 6 most ordered Samsung phones
    GET /api/featured/?brand=google      → Top 6 most ordered Google phones
    GET /api/featured/?brand=apple       → Top 6 most ordered iPhones

    Fully cached · Lightning fast · One endpoint to rule them all
    """
    serializer_class = PhoneListSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    pagination_class = None

    @method_decorator(cache_page(60 * 10))  # Cache response for 10 mins
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        brand_param = self.request.query_params.get('brand')
        limit = 6 if brand_param else 5

        # Dynamic cache key based on brand
        cache_key = f"featured_phones{'_' + brand_param.lower() if brand_param else ''}_v3"
        cached = cache.get(cache_key)

        if cached is not None:
            return cached

        # Base queryset
        queryset = Phone.objects.annotate(
            sales_count=Count(
                'orderitem',
                filter=Q(orderitem__order__status__in=['delivered', 'completed'])
            )
        )

        # Filter by brand if provided
        if brand_param:
            queryset = queryset.filter(brand__iexact=brand_param)

        # Order by sales (desc), then by ID (newest first)
        queryset = queryset.order_by('-sales_count', '-id')[:limit]

        # Fallback: if no sales yet, show newest phones
        if not queryset.exists():
            fallback = Phone.objects.all()
            if brand_param:
                fallback = fallback.filter(brand__iexact=brand_param)
            queryset = fallback.order_by('-id')[:limit]

        # Optimize for list view
        queryset = queryset.only('id', 'brand', 'model', 'thumbnail')

        # Cache result
        cache.set(cache_key, queryset, timeout=60 * 10)

        return queryset

class PhoneDetail(generics.RetrieveUpdateDestroyAPIView):
    """
    Retrieve, update, or delete a phone.

    - GET: Returns detailed phone information (read-only fields)
    - PUT/PATCH: Updates phone details (supports image uploads)
    - DELETE: Deletes the phone

    Caching is applied to GET requests.
    Cache invalidation happens on update and delete.
    """
    queryset = Phone.objects.all()
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    lookup_field = 'pk'
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        """
        Optimized base queryset with prefetching for storage variants and RAM.
        """
        return Phone.objects.prefetch_related(
            Prefetch(
                'storage_variants',
                queryset=StorageVariant.objects.select_related('ram')
            )
        )

    def get_serializer_class(self):
        """
        Use different serializers for read vs write operations.
        """
        if self.request.method == 'GET':
            return PhoneDetailSerializer
        return PhoneUpdateSerializer

    @method_decorator(cache_page(CACHE_TTL * 2))  # 2× longer cache for details
    def retrieve(self, request, *args, **kwargs):
        """
        Retrieve a phone with optimized caching.
        """
        return super().retrieve(request, *args, **kwargs)

    def get_object(self):
        """
        Retrieve phone instance with per-object caching.
        """
        obj = super().get_object()
        cache_key = f'phone_detail_optimized_{obj.id}'

        cached_obj = cache.get(cache_key)
        if cached_obj is not None:
            return cached_obj

        # Re-fetch with optimized queryset if cache miss
        fresh_obj = self.get_queryset().filter(id=obj.id).first()
        if fresh_obj:
            cache.set(cache_key, fresh_obj, timeout=CACHE_TTL * 2)

        return fresh_obj or obj

    def perform_update(self, serializer):
        """Update phone and aggressively clear caches for debugging."""
        instance = serializer.save()
        self._invalidate_phone_caches(instance.id)
        
        # Extra aggressive invalidation (temporary for debugging)
        cache.delete(f'phone_detail_optimized_{instance.id}')
        cache.delete('phone_list_cache_version')
        cache.delete('featured_phones_optimized')
        cache.clear()

    def perform_destroy(self, instance):
        """
        Delete phone instance and invalidate related caches.
        """
        phone_id = instance.id
        instance.delete()
        self._invalidate_phone_caches(phone_id)

    def _invalidate_phone_caches(self, phone_id):
        """
        Clear all caches that may contain this phone's data.
        """
        keys_to_delete = [
            f'phone_detail_optimized_{phone_id}',
            'featured_phones_optimized',
            'phone_list_cache_version',
            # Add any other phone-related cache keys here if needed
        ]
        cache.delete_many(keys_to_delete)

    def update(self, request, *args, **kwargs):
        """
        Custom update handler to support both PUT and PATCH correctly.
        """
        partial = kwargs.pop('partial', False)  # PATCH = partial
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if getattr(instance, '_prefetched_objects_cache', None):
            # If 'prefetch_related' has been applied to a queryset, we need to
            # forcibly invalidate the prefetch cache on the instance.
            instance._prefetched_objects_cache = {}

        return Response(serializer.data)


# ============================================================================
# ORDER VIEWS (Optimized)
# ============================================================================

class OrderList(generics.ListAPIView):
    """List orders with optimized queries and pagination."""
    serializer_class = OrderReadSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = OrderListPagination

    def get_queryset(self):
        """Filter orders with optimized prefetching."""
        queryset = Order.objects.select_related().prefetch_related(
            Prefetch('items',
                    queryset=OrderItem.objects.select_related('phone', 'memory_variant')
                                            .only('phone__brand', 'phone__model', 'phone__thumbnail',
                                                  'memory_variant__storage_capacity', 'quantity', 
                                                  'price_at_purchase'))
        ).order_by('-created_at')

        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        return queryset


class OrderDetail(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete an order."""
    queryset = Order.objects.select_related().prefetch_related('items')
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method in ['PUT', 'PATCH']:
            return OrderUpdateSerializer
        return OrderReadSerializer

    def get_queryset(self):
        return Order.objects.select_related().prefetch_related(
            Prefetch('items',
                    queryset=OrderItem.objects.select_related('phone', 'memory_variant'))
        )


class CreateOrder(generics.CreateAPIView):
    """Create a new order with optimized validation and email confirmation."""
    serializer_class = OrderWriteSerializer
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        """Validate and create order with email confirmation."""
        try:
            # Handle JSON data if needed
            if isinstance(request.data, str):
                try:
                    data = json.loads(request.data)
                except json.JSONDecodeError:
                    return Response(
                        {'status': 'error', 'message': 'Invalid JSON data'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                data = request.data.copy()

            # Set default values
            data.setdefault('status', 'pending')
            if 'estimated_delivery' not in data:
                data['estimated_delivery'] = (timezone.now() + timedelta(days=5)).isoformat()

            serializer = self.get_serializer(data=data)
            serializer.is_valid(raise_exception=True)
            
            with transaction.atomic():
                order = serializer.save()
                
                # Send email asynchronously (in real app, use Celery)
                try:
                    self._send_confirmation_email(order)
                except Exception as e:
                    logger.error(f"Email sending failed but order created: {str(e)}")

            return Response({
                'status': 'success',
                'order_number': order.order_number,
                'message': 'Order created successfully',
                'total_price': str(order.total_price)
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Order creation error: {str(e)}", exc_info=True)
            return Response(
                {'status': 'error', 'message': 'Failed to create order', 'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _send_confirmation_email(self, order):
        """Send order confirmation email."""
        subject = f"Order Confirmation - #{order.order_number}"
        context = {
            'order': order,
            'items': order.items.select_related('phone', 'memory_variant').all(),
            'total_price': order.total_price,
        }
        
        html_content = render_to_string('emails/order_confirmation.html', context)
        text_content = render_to_string('emails/order_confirmation.txt', context)
        recipient_list = [order.customer_email, 'itrade4phonesnow@gmail.com']

        email = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipient_list
        )
        email.attach_alternative(html_content, "text/html")
        
        try:
            email.send(fail_silently=False)
            logger.info(f"Sent confirmation for order #{order.order_number}")
        except Exception as e:
            logger.error(f"Failed to send email for order {order.order_number}: {str(e)}")

class VerifyPaymentView(APIView):
    """Verify payment status with Paystack."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        """Verify payment using Paystack API."""
        reference = request.data.get('reference')
        if not reference:
            return Response(
                {'status': 'error', 'message': 'Payment reference is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            headers = {
                'Authorization': f'Bearer {settings.PAYSTACK_SECRET_KEY}',
                'Content-Type': 'application/json'
            }
            response = requests.get(
                f'https://api.paystack.co/transaction/verify/{reference}',
                headers=headers,
                timeout=10  # Add timeout
            )
            response_data = response.json()

            if (response.status_code == 200 and 
                response_data.get('status') and 
                response_data['data']['status'] == 'success'):
                
                # Update order payment status if reference exists
                try:
                    order = Order.objects.get(payment_reference=reference)
                    order.payment_status = 'paid'
                    order.save()
                except Order.DoesNotExist:
                    pass  # Order might not exist yet or reference doesn't match
                
                return Response({
                    'status': 'success',
                    'message': 'Payment verified successfully',
                    'data': response_data['data']
                })
                
            return Response({
                'status': 'error',
                'message': 'Payment verification failed',
                'data': response_data.get('data', {})
            }, status=status.HTTP_400_BAD_REQUEST)
            
        except requests.Timeout:
            logger.error(f"Paystack API timeout for reference: {reference}")
            return Response(
                {'status': 'error', 'message': 'Payment verification timeout'},
                status=status.HTTP_408_REQUEST_TIMEOUT
            )
        except Exception as e:
            logger.error(f"Payment verification error: {str(e)}")
            return Response(
                {'status': 'error', 'message': 'Failed to verify payment'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ============================================================================
# VARIANT VIEWS (Optimized)
# ============================================================================

class StorageVariantList(generics.ListCreateAPIView):
    """
    GET: List storage variants (optionally filtered by ?phone=ID)
    POST: Create a new storage variant
    """
    permission_classes = [permissions.IsAuthenticated]  # or IsAuthenticatedOrReadOnly

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return StorageVariantWriteSerializer  # writable for create
        return StorageVariantListSerializer       # lightweight for list

    def get_queryset(self):
        qs = StorageVariant.objects.select_related('ram', 'phone').order_by('id')
        
        phone_id = self.request.query_params.get('phone')
        if phone_id:
            try:
                qs = qs.filter(phone_id=int(phone_id))
            except ValueError:
                pass
        
        return qs

class RAMVariantList(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    pagination_class = None
    
    def get_serializer_class(self):
        if self.request.method in ['POST', 'PUT', 'PATCH']:
            return RAMVariantDetailSerializer
        return RAMVariantListSerializer

    def get_queryset(self):
        qs = RAMVariant.objects.select_related('phone').only('id', 'ram', 'phone')
        phone_id = self.request.query_params.get('phone')
        if phone_id:
            qs = qs.filter(phone_id=phone_id)
        return qs

    def perform_create(self, serializer):
        instance = serializer.save()
        # Optional cache invalidation
        # cache.delete_many([
        #     f'phone_detail_optimized_{instance.phone.id}',
        #     'phone_list_cache_version',
        # ])

class StorageSelector(generics.ListAPIView):
    """List storage variants for a phone, optionally filtered by RAM."""
    serializer_class = StorageVariantListSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    @method_decorator(cache_page(CACHE_TTL))
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def get_queryset(self):
        """Retrieve storage variants with optimized query and caching."""
        phone_id = self.kwargs.get('phone_id')
        ram_id = self.kwargs.get('ram_id')
        cache_key = f'storage_selector_optimized_{phone_id}_{ram_id}'
        
        queryset = cache.get(cache_key)
        if not queryset:
            phone = get_object_or_404(Phone, id=phone_id)
            queryset = StorageVariant.objects.filter(phone=phone).select_related('ram')
            
            if ram_id:
                ram_variant = get_object_or_404(RAMVariant, id=ram_id, phone=phone)
                queryset = queryset.filter(ram=ram_variant)
            
            cache.set(cache_key, queryset, timeout=CACHE_TTL)
            
        return queryset

    def list(self, request, *args, **kwargs):
        """Return storage and RAM variants for a phone."""
        phone_id = self.kwargs.get('phone_id')
        ram_id = self.kwargs.get('ram_id')
        cache_key = f'storage_selector_full_optimized_{phone_id}_{ram_id}'
        
        response_data = cache.get(cache_key)
        if not response_data:
            storage_variants = self.get_queryset()
            phone = get_object_or_404(Phone, id=phone_id)
            
            if ram_id:
                ram_variants = [get_object_or_404(RAMVariant, id=ram_id, phone=phone)]
            else:
                ram_variants = RAMVariant.objects.filter(phone=phone).only('id', 'ram')
            
            response_data = {
                'storage_variants': self.get_serializer(storage_variants, many=True).data,
                'ram_variants': RAMVariantListSerializer(ram_variants, many=True).data,
            }
            cache.set(cache_key, response_data, timeout=CACHE_TTL)
            
        return Response(response_data)


class BrandSelector(generics.ListAPIView):
    """List phones by brand with optimized query."""
    serializer_class = PhoneListSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        brand = self.kwargs.get('brand')
        return Phone.objects.filter(brand__iexact=brand).prefetch_related(
            Prefetch('storage_variants',
                    queryset=StorageVariant.objects.select_related('ram')
                                                  .only('storage_capacity', 'price', 'old_price', 'ram__ram'))
        ).only(
            'id', 'brand', 'model', 'thumbnail', 'battery_capacity', 'display_size'
        )

class StorageVariantDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = StorageVariant.objects.select_related('ram', 'phone')
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = 'pk'

    def get_serializer_class(self):
        if self.request.method in ['GET', 'HEAD', 'OPTIONS']:
            return StorageVariantListSerializer     # or StorageVariantDetailSerializer — whichever you prefer for read
        return StorageVariantWriteSerializer        # ← use this for PUT/PATCH

    def perform_update(self, serializer):
        instance = serializer.save()
        print("After save - price:", instance.price, "old_price:", instance.old_price, "stock:", instance.stock)
        
        # Safe cache invalidation
        cache.delete(f'phone_detail_optimized_{instance.phone.id}')
        cache.delete('phone_list_cache_version')
        cache.delete('featured_phones_optimized')
        cache.delete(f'storage_selector_optimized_{instance.phone.id}_None')

    def update(self, request, *args, **kwargs):
        print("Entering update() - content_type:", request.content_type)
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        print("Serializer is valid?", serializer.is_valid())
        print("Serializer errors:", serializer.errors)
        print("Validated data:", serializer.validated_data)
        return super().update(request, *args, **kwargs)
    
# ============================================================================
# CONTACT VIEWS (Optimized)
# ============================================================================

class ContactMessageCreate(generics.CreateAPIView):
    """Create a new contact message."""
    serializer_class = ContactMessageCreateSerializer
    permission_classes = [AllowAny]


class ContactMessageList(generics.ListAPIView):
    """List all contact messages, ordered by creation date."""
    serializer_class = ContactMessageSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        return ContactMessage.objects.all().order_by('-created_at')


class ContactMessageDetail(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a contact message."""
    queryset = ContactMessage.objects.all()
    serializer_class = ContactMessageSerializer
    permission_classes = [IsAuthenticated]


class ContactMessageStatusUpdate(generics.UpdateAPIView):
    """Update the status of a contact message."""
    queryset = ContactMessage.objects.all()
    serializer_class = ContactMessageStatusSerializer
    permission_classes = [IsAuthenticated]


# ============================================================================
# ANALYTICS VIEWS
# ============================================================================

class AnalyticsVisitsView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Return analytics data for visits."""
        # Implementation depends on your analytics requirements
        return Response({"message": "Analytics endpoint"})
    """Retrieve website visit analytics for authenticated users."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Return total visits, daily visits, and percentage change."""
        try:
            now = timezone.now()
            one_week_ago = now - timedelta(days=7)
            one_month_ago = now - timedelta(days=30)

            # Total visits
            total_visits = Visit.objects.filter(timestamp__lte=now).count()

            # Daily visits (last 30 days)
            daily_visits = (
                Visit.objects.filter(timestamp__gte=one_month_ago, timestamp__lte=now)
                .annotate(date=TruncDate('timestamp'))
                .values('date')
                .annotate(count=Count('id'))
                .order_by('date')
            )
            daily_data = {visit['date'].strftime('%Y-%m-%d'): visit['count'] for visit in daily_visits}

            # Percentage change (last 7 days vs previous 7 days)
            current_period = Visit.objects.filter(timestamp__gte=one_week_ago, timestamp__lte=now).count()
            previous_period = Visit.objects.filter(timestamp__gte=now - timedelta(days=14), timestamp__lt=one_week_ago).count()
            change_percent = round(((current_period - previous_period) / previous_period) * 100) if previous_period > 0 else 0

            response_data = {
                'count': total_visits,
                'daily': daily_data,
                'change_percent': change_percent,
                'period': '7days'
            }
            return Response(response_data)
        except Exception as e:
            logger.error(f"Analytics visits error: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)