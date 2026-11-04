# store/views.py
import os

from django.conf import settings
from django.contrib.auth import login, logout
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.db import transaction
from django.db.models import Prefetch, Count, Q, Sum, F, DecimalField, ExpressionWrapper, Max, OuterRef, Subquery
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers
from django.core.cache import cache
from django.http import HttpResponse
import csv
from rest_framework import generics, status, permissions
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from rest_framework.authentication import TokenAuthentication, SessionAuthentication
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from .models import Visit, ContactMessage, Phone, Order, StorageVariant, RAMVariant, OrderItem, OrderEvent, SecurityEvent, StoreSetting, MessageReply, AdminAuditEvent
from .serializers import (
    PhoneAdminListSerializer, PhoneListSerializer, PhoneDetailSerializer,
    OrderWriteSerializer, OrderReadSerializer, OrderUpdateSerializer,
    ContactMessageSerializer, ContactMessageCreateSerializer, ContactMessageStatusSerializer, PhoneUpdateSerializer, StorageVariantDetailSerializer,
    StorageVariantListSerializer, RAMVariantListSerializer, RAMVariantDetailSerializer, StorageVariantWriteSerializer,
    UserLoginSerializer
)
from .throttles import LoginThrottle, TrackThrottle
from .utils import get_client_ip, hash_ip

import hashlib
import hmac
import json
import logging
from decimal import Decimal
import requests
from datetime import timedelta
import time

# Configuration
CACHE_TTL = getattr(settings, 'CACHE_TTL', 60 * 15)  # 15-minute cache timeout
logger = logging.getLogger(__name__)

# ---- Paystack helpers ------------------------------------------------------
def to_pesewas(amount):
    return int((Decimal(amount) * 100).to_integral_value())

PAYSTACK_KEY = getattr(settings, 'PAYSTACK_SECRET_KEY', None) or getattr(settings, 'PAYSTACK_SECRET_TEST', '')
def fetch_paystack_transaction(reference):
    """Return Paystack's data for a successful payment, or None if it did not succeed.
    Raises requests.RequestException if Paystack can't be reached."""
    resp = requests.get(
        f'https://api.paystack.co/transaction/verify/{reference}',
        headers={'Authorization': f'Bearer {PAYSTACK_KEY}'},
        timeout=10,
    )
    try:
        body = resp.json()
    except ValueError:
        return None
    data = body.get('data') or {}
    if resp.status_code == 200 and body.get('status') and data.get('status') == 'success':
        return data
    return None


# ---- Cache invalidation ----------------------------------------------------
FEATURED_KEYS = (
    'featured_phones_v4',
    'featured_phones_samsung_v4',
    'featured_phones_google_v4',
)
LIST_VERSION_KEY = 'phone_list_cache_version'


def invalidate_phone_caches(phone_id=None):
    """Call after anything that changes what shoppers see (phone or variant writes)."""
    keys = list(FEATURED_KEYS)
    if phone_id:
        keys += [f'phone_detail:{phone_id}', f'storage_selector:{phone_id}']
    cache.delete_many(keys)
    try:
        cache.incr(LIST_VERSION_KEY)
    except ValueError:
        cache.set(LIST_VERSION_KEY, 2, timeout=None)  # version keys must never expire


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
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request):
        serializer = UserLoginSerializer(data=request.data)
        if not serializer.is_valid():
            try:
                SecurityEvent.objects.create(kind='login_fail', ip_hash=hash_ip(get_client_ip(request)), path='/auth/login/', severity='medium')
            except Exception:
                logger.exception('Could not record failed login security event')
            return Response(
                {'status': 'error', 'message': serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = serializer.validated_data['user']
        token, _ = Token.objects.get_or_create(user=user)

        return Response({
            'status': 'success',
            'token': token.key,
            'user': {
                'id':           user.id,
                'username':     user.username,
                'email':        user.email,
                'is_superuser': user.is_superuser,
            }
        })


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
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    pagination_class = None

    def get_serializer_class(self):
        if self.request.method == 'GET':
            return PhoneListSerializer
        return PhoneDetailSerializer

    def get_queryset(self):
        from .models import StorageVariant

        queryset = Phone.objects.filter(is_active=True).prefetch_related(
            Prefetch(
                'storage_variants',
                queryset=StorageVariant.objects.only(
                    'id', 'phone_id', 'storage_capacity',
                    'price', 'old_price', 'availability'
                ).order_by('-availability', 'storage_capacity'),
            )
        ).order_by('id')

        brand = self.request.query_params.get('brand')
        if brand:
            queryset = queryset.filter(brand__iexact=brand)

        return queryset

    def list(self, request, *args, **kwargs):
        version = cache.get(LIST_VERSION_KEY, 1)
        params = request.query_params.urlencode()
        cache_key = f'phone_list:v{version}:{params}'

        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response(cached_data)

        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        data = serializer.data

        cache.set(cache_key, data, timeout=CACHE_TTL)
        return Response(data)

    def perform_create(self, serializer):
        instance = serializer.save()
        invalidate_phone_caches(instance.id)



class FeaturedList(generics.ListAPIView):
    serializer_class = PhoneListSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    pagination_class = None

    def get_queryset(self):
        from .models import StorageVariant

        queryset = Phone.objects.filter(is_active=True).annotate(
            sales_count=Count(
                'orderitem',
                filter=Q(orderitem__order__status__in=['delivered', 'completed'])
            )
        ).prefetch_related(
            Prefetch(
                'storage_variants',
                queryset=StorageVariant.objects.only(
                    'id', 'phone_id', 'storage_capacity',
                    'price', 'old_price', 'availability'
                ).order_by('-availability', 'storage_capacity'),
            )
        )

        brand_param = self.request.query_params.get('brand')
        if brand_param:
            queryset = queryset.filter(brand__iexact=brand_param)

        return queryset

    def list(self, request, *args, **kwargs):
        from .models import StorageVariant

        brand_param = request.query_params.get('brand')
        limit = 6 if brand_param else 5
        cache_key = f"featured_phones{'_' + brand_param.lower() if brand_param else ''}_v4"

        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response(cached_data)

        phones = list(self.get_queryset().order_by('-sales_count', '-id')[:limit])

        # Fallback: no sales yet → newest phones
        if not phones:
            fallback_qs = Phone.objects.filter(is_active=True).prefetch_related(
                Prefetch(
                    'storage_variants',
                    queryset=StorageVariant.objects.only(
                        'id', 'phone_id', 'storage_capacity',
                        'price', 'old_price', 'availability'
                    ).order_by('storage_capacity'),
                )
            )
            if brand_param:
                fallback_qs = fallback_qs.filter(brand__iexact=brand_param)
            phones = list(fallback_qs.order_by('-id')[:limit])

        serializer = self.get_serializer(phones, many=True)
        data = serializer.data

        cache.set(cache_key, data, timeout=60 * 10)
        return Response(data)


class AdminPhonePagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 50

class AdminPhoneList(generics.ListAPIView):
    serializer_class   = PhoneAdminListSerializer
    permission_classes = [permissions.IsAdminUser]
    pagination_class   = AdminPhonePagination

    def get_queryset(self):
        from .models import StorageVariant

        include_inactive = self.request.query_params.get('include_inactive')

        queryset = Phone.objects.prefetch_related(
            Prefetch(
                'storage_variants',
                queryset=StorageVariant.objects.only(
                    'id', 'phone_id', 'storage_capacity',
                    'price', 'old_price', 'availability'
                ).order_by('-availability', 'storage_capacity'),
            )
        ).order_by('id')

        if not include_inactive:
            queryset = queryset.filter(is_active=True)

        brand = self.request.query_params.get('brand')
        if brand:
            queryset = queryset.filter(brand__iexact=brand)

        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(model__icontains=search) |
                Q(brand__icontains=search)
            )

        return queryset


class PhoneDetail(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    lookup_field = 'pk'
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        return Phone.objects.filter(is_active=True).only(
            'battery_capacity', 'display_size', 'camera_details', 'physical_sim',
            'esim', 'number_of_sims', 'link'
        )

    def get_serializer_class(self):
        if self.request.method == 'GET':
            return PhoneDetailSerializer
        return PhoneUpdateSerializer

    def retrieve(self, request, *args, **kwargs):
        pk = self.kwargs.get('pk')
        cache_key = f'phone_detail:{pk}'

        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response(cached_data)

        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = serializer.data

        cache.set(cache_key, data, timeout=CACHE_TTL * 2)
        return Response(data)

    def perform_update(self, serializer):
        instance = serializer.save()
        invalidate_phone_caches(instance.id)

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=['is_active'])
        invalidate_phone_caches(instance.id)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        if getattr(instance, '_prefetched_objects_cache', None):
            instance._prefetched_objects_cache = {}
        return Response(serializer.data)

class PhoneRestore(APIView):
    permission_classes = [permissions.IsAdminUser]

    def post(self, request, pk):
        phone = get_object_or_404(Phone, pk=pk, is_active=False)
        phone.is_active = True
        phone.save(update_fields=['is_active'])
        invalidate_phone_caches(phone.id)
        return Response({'status': 'restored'})


    
# ============================================================================
# ORDER VIEWS (Optimized)
# ============================================================================

class OrderList(generics.ListAPIView):
    """List orders with optimized queries and pagination."""
    serializer_class = OrderReadSerializer
    permission_classes = [permissions.IsAdminUser]
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

        params = self.request.query_params
        status_filter = params.get('status')
        if status_filter and status_filter != 'all':
            queryset = queryset.filter(status=status_filter)
        search = (params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(
                Q(order_number__icontains=search) |
                Q(customer_name__icontains=search) |
                Q(customer_email__icontains=search) |
                Q(customer_phone_number__icontains=search) |
                Q(town__icontains=search) | Q(region__icontains=search)
            )
        ordering = params.get('ordering', '-created_at')
        allowed = {'created_at', '-created_at', 'status', '-status', 'customer_name', '-customer_name', 'status_changed_at', '-status_changed_at'}
        queryset = queryset.order_by(ordering if ordering in allowed else '-created_at')
        return queryset


class OrderDetail(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete an order."""
    queryset = Order.objects.select_related().prefetch_related('items')
    permission_classes = [permissions.IsAdminUser]

    def get_serializer_class(self):
        if self.request.method in ['PUT', 'PATCH']:
            return OrderUpdateSerializer
        return OrderReadSerializer

    def get_queryset(self):
        return Order.objects.select_related().prefetch_related(
            Prefetch('items',
                    queryset=OrderItem.objects.select_related('phone', 'memory_variant'))
        )

    def perform_update(self, serializer):
        instance = self.get_object()
        old_status = instance.status
        old_note = instance.admin_note
        old_cash_collected = instance.cash_collected
        updated = serializer.save(status_changed_at=timezone.now() if 'status' in serializer.validated_data and serializer.validated_data.get('status') != old_status else instance.status_changed_at)
        if updated.status != old_status:
            OrderEvent.objects.create(order=updated, actor=self.request.user, event_type='status_changed', old_value=old_status, new_value=updated.status)
        if updated.admin_note != old_note:
            OrderEvent.objects.create(order=updated, actor=self.request.user, event_type='admin_note_updated', old_value=old_note, new_value=updated.admin_note)
        if updated.cash_collected != old_cash_collected:
            OrderEvent.objects.create(order=updated, actor=self.request.user, event_type='cash_collected_changed', old_value=str(old_cash_collected), new_value=str(updated.cash_collected))


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

            # Respect the admin store pause switch for public checkout.
            store_controls = StoreSetting.objects.filter(pk=1).first()
            if store_controls and store_controls.orders_paused:
                return Response({'status': 'error', 'message': 'Online ordering is temporarily paused. Please try again later.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

            # Set default values
            data.setdefault('status', 'pending')
            if 'estimated_delivery' not in data:
                data['estimated_delivery'] = (timezone.now() + timedelta(days=5)).isoformat()

            serializer = self.get_serializer(data=data)
            serializer.is_valid(raise_exception=True)

            # Never trust the browser about payment: confirm it with Paystack here.
            validated = serializer.validated_data
            extra = {}
            if validated['payment_method'] == 'cash_on_delivery':
                extra['payment_reference'] = None
            else:
                reference = (validated.get('payment_reference') or '').strip()
                if not reference:
                    raise ValidationError({'payment_reference': 'Payment is required for this payment method.'})
                if Order.objects.filter(payment_reference=reference).exists():
                    raise ValidationError({'payment_reference': 'This payment has already been used for an order.'})
                try:
                    txn = fetch_paystack_transaction(reference)
                except requests.RequestException:
                    logger.error("Paystack unreachable while creating order (ref %s)", reference)
                    return Response({
                        'status': 'error',
                        'message': 'We could not confirm your payment right now. Please try again in a moment. Do not pay again.',
                    }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

                total = sum(i['memory_variant'].price * i['quantity'] for i in validated['items'])
                if (txn is None
                        or txn.get('currency') != 'GHS'
                        or txn.get('amount', 0) != to_pesewas(total)):
                    logger.warning("Payment check failed for reference %s (expected %s pesewas)",
                                   reference, to_pesewas(total))
                    raise ValidationError({'payment_reference': 'Payment could not be verified for this order total.'})
                extra['payment_status'] = 'paid'

            with transaction.atomic():
                order = serializer.save(**extra)
                
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

        except ValidationError:
            raise  # DRF turns this into a proper 400 with field errors
        except Exception as e:
            logger.error(f"Order creation error: {str(e)}", exc_info=True)
            return Response(
                {'status': 'error', 'message': 'Failed to create order'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _send_confirmation_email(self, order):
        """Send order confirmation email."""
        subject = f"Order Confirmation - #{order.order_number}"
        context = {
            'order': order,
            'items': order.items.select_related('phone', 'memory_variant').all(),
            'total_price': order.total_price,
            'delivery_address': f"{order.street_address}, {order.town}, {order.region}",
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
                'Authorization': f'Bearer {PAYSTACK_KEY}',
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
                
                # If an order already carries this reference, the amount Paystack
                # collected (in pesewas) must match the order total.
                order = Order.objects.filter(payment_reference=reference).first()
                if order is not None:
                    expected = int(Decimal(order.total_price) * 100)
                    if response_data['data'].get('currency') != 'GHS' or response_data['data'].get('amount', 0) != expected:
                        logger.warning(
                            "Amount mismatch for order %s: paid=%s expected=%s",
                            order.order_number, response_data['data'].get('amount'), expected,
                        )
                        return Response({
                            'status': 'error',
                            'message': 'Payment amount does not match order total',
                        }, status=status.HTTP_400_BAD_REQUEST)
                    if order.payment_status != 'paid':
                        order.payment_status = 'paid'
                        order.save(update_fields=['payment_status'])
                
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
    pagination_class = None
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

    def perform_create(self, serializer):
        instance = serializer.save()
        invalidate_phone_caches(instance.phone_id)


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
    serializer_class = StorageVariantListSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        phone_id = self.kwargs.get('phone_id')
        return StorageVariant.objects.filter(
            phone_id=phone_id
        ).only(
            'id', 'phone_id', 'storage_capacity',
            'price', 'old_price', 'availability'
        ).order_by('-availability', 'storage_capacity')

    def list(self, request, *args, **kwargs):
        phone_id = self.kwargs.get('phone_id')
        cache_key = f'storage_selector:{phone_id}'

        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        variants = self.get_queryset()
        data = {
            'storage_variants': self.get_serializer(variants, many=True).data,
        }

        cache.set(cache_key, data, timeout=CACHE_TTL)
        return Response(data)


class StorageVariantDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = StorageVariant.objects.select_related('ram', 'phone')
    permission_classes = [permissions.IsAdminUser]
    lookup_field = 'pk'

    def get_serializer_class(self):
        if self.request.method in ['GET', 'HEAD', 'OPTIONS']:
            return StorageVariantListSerializer     # or StorageVariantDetailSerializer — whichever you prefer for read
        return StorageVariantWriteSerializer        # ← use this for PUT/PATCH

    def perform_update(self, serializer):
        before = self.get_object()
        old_values = {'price': str(before.price), 'old_price': str(before.old_price) if before.old_price is not None else None, 'stock': before.stock, 'availability': before.availability}
        instance = serializer.save()
        new_values = {'price': str(instance.price), 'old_price': str(instance.old_price) if instance.old_price is not None else None, 'stock': instance.stock, 'availability': instance.availability}
        AdminAuditEvent.objects.create(actor=self.request.user, action='variant_updated', object_type='StorageVariant', object_id=str(instance.pk), details={'before': old_values, 'after': new_values})
        invalidate_phone_caches(instance.phone_id)


    def perform_destroy(self, instance):
        phone_id = instance.phone_id
        instance.delete()
        invalidate_phone_caches(phone_id)

    
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

# ============================================================================
# CONTACT VIEWS (Optimized)
# ============================================================================

class ContactMessageCreate(generics.CreateAPIView):
    """Create a new contact message."""
    serializer_class = ContactMessageCreateSerializer
    permission_classes = [AllowAny]


class ContactMessageList(generics.ListAPIView):
    """List admin messages with server-side status, search and ordering filters."""
    serializer_class = ContactMessageSerializer
    permission_classes = [permissions.IsAdminUser]
    pagination_class = None

    def get_queryset(self):
        queryset = ContactMessage.objects.all()
        params = self.request.query_params
        status_filter = params.get('status')
        if status_filter and status_filter != 'all':
            queryset = queryset.filter(status=status_filter)
        search = (params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(email__icontains=search) |
                Q(subject__icontains=search) | Q(phone__icontains=search) |
                Q(message__icontains=search)
            )
        ordering = params.get('ordering', '-created_at')
        allowed = {'created_at', '-created_at', 'name', '-name', 'subject', '-subject'}
        return queryset.order_by(ordering if ordering in allowed else '-created_at')


class ContactMessageDetail(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a contact message."""
    queryset = ContactMessage.objects.all()
    serializer_class = ContactMessageSerializer
    permission_classes = [permissions.IsAdminUser]


class ContactMessageStatusUpdate(generics.UpdateAPIView):
    """Update the status of a contact message."""
    queryset = ContactMessage.objects.all()
    serializer_class = ContactMessageStatusSerializer
    permission_classes = [permissions.IsAdminUser]


class AdminCountsView(APIView):
    permission_classes = [permissions.IsAdminUser]
    CACHE_KEY = 'admin:counts:v2'
    CACHE_TTL = 5

    @staticmethod
    def _unique_visitors(queryset):
        sessions = queryset.exclude(Q(session_id__isnull=True) | Q(session_id='')).values('session_id').distinct().count()
        fallback_ips = queryset.filter(Q(session_id__isnull=True) | Q(session_id='')).exclude(
            Q(ip_address__isnull=True) | Q(ip_address='')
        ).values('ip_address').distinct().count()
        return sessions + fallback_ips

    def get(self, request):
        data = cache.get(self.CACHE_KEY)
        if data is None:
            today = timezone.localdate()
            visits = Visit.objects.filter(is_bot=False)
            today_visits = visits.filter(timestamp__date=today)
            data = {
                'orders_total': Order.objects.count(),
                'orders_pending': Order.objects.filter(status='pending').count(),
                'orders_unconfirmed': Order.objects.filter(status__in=['pending', 'confirmed']).count(),
                'messages_new': ContactMessage.objects.filter(status='new').count(),
                'visitors_total': self._unique_visitors(visits),
                'visitors_today': self._unique_visitors(today_visits),
                'page_views_total': visits.count(),
                'generated_at': timezone.now().isoformat(),
            }
            cache.set(self.CACHE_KEY, data, self.CACHE_TTL)
        response = Response(data)
        response['Cache-Control'] = 'no-store'
        return response


class AdminSummaryView(APIView):
    permission_classes = [permissions.IsAdminUser]
    CACHE_TTL = 30

    @staticmethod
    def _unique_visitors(queryset):
        sessions = queryset.exclude(Q(session_id__isnull=True) | Q(session_id='')).values('session_id').distinct().count()
        fallback_ips = queryset.filter(Q(session_id__isnull=True) | Q(session_id='')).exclude(
            Q(ip_address__isnull=True) | Q(ip_address='')
        ).values('ip_address').distinct().count()
        return sessions + fallback_ips

    def get(self, request):
        range_key = request.query_params.get('range', '7d')
        days = {'7d': 7, '30d': 30, '90d': 90}.get(range_key, 7)
        cache_key = f'admin:summary:v2:{days}'
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        now = timezone.now()
        start = now - timedelta(days=days)
        previous_start = start - timedelta(days=days)
        current = Order.objects.filter(created_at__gte=start)
        previous = Order.objects.filter(created_at__gte=previous_start, created_at__lt=start)
        revenue_filter = Q(payment_status='paid') | Q(payment_method='cash_on_delivery', cash_collected=True)
        revenue_expression = ExpressionWrapper(
            F('items__price_at_purchase') * F('items__quantity'),
            output_field=DecimalField(max_digits=14, decimal_places=2)
        )
        current_revenue = current.filter(revenue_filter).aggregate(v=Sum(revenue_expression))['v'] or Decimal('0')
        previous_revenue = previous.filter(revenue_filter).aggregate(v=Sum(revenue_expression))['v'] or Decimal('0')
        orders_count = current.count()
        previous_count = previous.count()
        paid_order_count = current.filter(revenue_filter).distinct().count()
        pending_count = current.filter(status='pending').count()
        aov = current_revenue / paid_order_count if paid_order_count else Decimal('0')
        top_phones = list(current.values('items__phone__brand', 'items__phone__model').annotate(
            quantity=Sum('items__quantity'), revenue=Sum(revenue_expression)
        ).order_by('-quantity')[:5])
        payment_split = list(current.values('payment_method').annotate(count=Count('id')).order_by('-count'))
        regions = list(current.values('region').annotate(count=Count('id')).order_by('-count')[:10])

        current_visits = Visit.objects.filter(is_bot=False, timestamp__gte=start, timestamp__lt=now)
        previous_visits = Visit.objects.filter(is_bot=False, timestamp__gte=previous_start, timestamp__lt=start)
        visitors = self._unique_visitors(current_visits)
        previous_visitors = self._unique_visitors(previous_visits)
        page_views = current_visits.count()

        def pct_change(a, b):
            if not b:
                return 100 if a else 0
            return round((float(a - b) / float(b)) * 100, 1)

        data = {
            'range': range_key, 'days': days,
            'orders': orders_count, 'orders_change_percent': pct_change(orders_count, previous_count),
            'revenue': str(current_revenue), 'revenue_change_percent': pct_change(current_revenue, previous_revenue),
            'average_order_value': str(aov), 'pending_orders': pending_count,
            'visitors': visitors, 'previous_visitors': previous_visitors,
            'visitors_change_percent': pct_change(visitors, previous_visitors),
            'page_views': page_views,
            'top_phones': top_phones, 'payment_split': payment_split, 'regions': regions,
            'generated_at': now.isoformat(),
        }
        cache.set(cache_key, data, self.CACHE_TTL)
        return Response(data)


# ============================================================================
# ANALYTICS VIEWS
# ============================================================================

class AnalyticsVisitsView(APIView):
    permission_classes = [permissions.IsAdminUser]
    CACHE_KEY = 'analytics:visits'
    CACHE_TTL = 60

    def get(self, request):
        cached = cache.get(self.CACHE_KEY)
        if cached is not None:
            return Response(cached)

        now = timezone.now()
        week_ago = now - timedelta(days=7)
        two_weeks_ago = now - timedelta(days=14)
        month_ago = now - timedelta(days=30)

        humans = Visit.objects.filter(is_bot=False)

        # One query for every headline number
        totals = humans.aggregate(
            total=Count('id'),
            current=Count('id', filter=Q(timestamp__gte=week_ago)),
            previous=Count('id', filter=Q(timestamp__gte=two_weeks_ago, timestamp__lt=week_ago)),
            unique_30d=Count('ip_address', distinct=True, filter=Q(timestamp__gte=month_ago)),
        )

        # One query for the daily series
        daily_rows = (
            humans.filter(timestamp__gte=month_ago)
            .annotate(date=TruncDate('timestamp'))
            .values('date')
            .annotate(count=Count('id'))
            .order_by('date')
        )
        daily = {row['date'].strftime('%Y-%m-%d'): row['count'] for row in daily_rows}

        current, previous = totals['current'], totals['previous']
        if previous > 0:
            change = round((current - previous) / previous * 100)
        else:
            change = 100 if current > 0 else 0

        data = {
            'count': totals['total'],
            'unique_30d': totals['unique_30d'],
            'daily': daily,
            'change_percent': change,
            'period': '7days',
        }
        cache.set(self.CACHE_KEY, data, self.CACHE_TTL)
        return Response(data)


class TrackVisitView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [TrackThrottle]

    def post(self, request):
        path = str(request.data.get('path') or '')[:255]
        event = str(request.data.get('event') or 'page_view')[:32]
        if event not in {'page_view', 'product_view', 'add_to_cart', 'checkout_start', 'purchase'}:
            return Response({'detail': 'Invalid event.'}, status=400)

        user_agent = request.META.get('HTTP_USER_AGENT', '')[:255]
        referrer = (
            request.data.get('referrer')
            or request.META.get('HTTP_REFERER')
            or ''
        )[:200]

        if (
            not path.startswith('/')
            or path.startswith('/admin')
            or Visit.is_bot_user_agent(user_agent)
        ):
            return Response(status=204)

        ip = get_client_ip(request)
        session_id = str(request.data.get('session_id') or '')[:40]
        product_id = request.data.get('product_id') if str(request.data.get('product_id', '')).isdigit() else None
        cache_key = f"trk:{hash_ip(ip)}:{session_id}:{event}:{path}:{product_id or ''}"

        if not cache.add(cache_key, 1, 30):
            return Response(status=204)

        Visit.objects.create(
            ip_address=ip,
            user_agent=user_agent,
            session_id=session_id or None,
            referrer=referrer,
            path=path,
            event=event,
            utm_source=str(request.data.get('utm_source') or '')[:100],
            utm_medium=str(request.data.get('utm_medium') or '')[:100],
            utm_campaign=str(request.data.get('utm_campaign') or '')[:150],
            product_id=product_id,
        )
        return Response(status=204)

        return Response(status=204)



# ==========================================================================
# PHASE 2: PAYMENT RECONCILIATION, BULK VARIANTS, CUSTOMERS, DELIVERY EXPORT
# ==========================================================================


class AdminClonePhoneView(APIView):
    """Clone a phone record together with its RAM and storage variants."""
    permission_classes = [permissions.IsAdminUser]

    def post(self, request, pk):
        source = get_object_or_404(Phone.objects.prefetch_related('ram_variants__storage_variants'), pk=pk)
        requested_model = str(request.data.get('model') or f'{source.model} Copy').strip()[:20]
        if not requested_model:
            return Response({'detail': 'A model name is required for the clone.'}, status=400)
        with transaction.atomic():
            clone = Phone.objects.create(
                brand=source.brand, model=requested_model,
                physical_sim=source.physical_sim, esim=source.esim,
                number_of_sims=source.number_of_sims, support_5G=source.support_5G,
                Fingerprint=source.Fingerprint, FaceScanner=source.FaceScanner,
                Iris_scanner=source.Iris_scanner, display_size=source.display_size,
                camera_details=source.camera_details, battery_capacity=source.battery_capacity,
                thumbnail=source.thumbnail.name if source.thumbnail else None,
                photo_1=source.photo_1.name if source.photo_1 else None,
                photo_2=source.photo_2.name if source.photo_2 else None,
                photo_3=source.photo_3.name if source.photo_3 else None,
                link=source.link, is_active=False,
            )
            for ram in source.ram_variants.all():
                new_ram = RAMVariant.objects.create(phone=clone, ram=ram.ram)
                for variant in ram.storage_variants.all():
                    StorageVariant.objects.create(
                        phone=clone, ram=new_ram, storage_capacity=variant.storage_capacity,
                        availability=False,
                        price=variant.price, old_price=variant.old_price,
                    )
        invalidate_phone_caches(clone.id)
        return Response({'id': clone.id, 'brand': clone.brand, 'model': clone.model, 'is_active': clone.is_active, 'message': 'Phone and variants cloned. The new phone is inactive until you publish it.'}, status=status.HTTP_201_CREATED)

class AdminPaymentReconciliationView(APIView):
    """Admin queue for unpaid Paystack orders and aged pending payments."""
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        qs = Order.objects.all().order_by('created_at')
        payment_status = request.query_params.get('payment_status', 'pending')
        if payment_status in ('pending', 'paid', 'failed'):
            qs = qs.filter(payment_status=payment_status)
        method = request.query_params.get('payment_method')
        if method:
            qs = qs.filter(payment_method=method)
        overdue_only = request.query_params.get('overdue', 'true').lower() == 'true'
        if payment_status == 'pending':
            qs = qs.filter(payment_method='mobile_money').exclude(payment_reference__isnull=True).exclude(payment_reference='')
            if overdue_only:
                qs = qs.filter(created_at__lte=timezone.now() - timedelta(hours=1))
        rows = []
        for order in qs.prefetch_related('items__phone', 'items__memory_variant')[:500]:
            rows.append({
                'id': order.id, 'order_number': order.order_number,
                'customer_name': order.customer_name,
                'customer_phone_number': order.customer_phone_number,
                'payment_method': order.payment_method,
                'payment_status': order.payment_status,
                'payment_reference': order.payment_reference,
                'total_price': str(order.total_price),
                'created_at': order.created_at.isoformat(),
                'age_hours': round((timezone.now() - order.created_at).total_seconds() / 3600, 1),
                'status': order.status,
            })
        return Response({'results': rows, 'count': len(rows)})


class AdminVerifyOrderPaymentView(APIView):
    """Manually reconcile one order against Paystack using exact currency/amount."""
    permission_classes = [permissions.IsAdminUser]

    def post(self, request, pk):
        order = get_object_or_404(Order, pk=pk)
        if order.payment_method != 'mobile_money':
            return Response({'detail': 'Only mobile-money Paystack orders can be verified through this endpoint.'}, status=400)
        if not PAYSTACK_KEY:
            return Response({'detail': 'Paystack secret is not configured on the server.'}, status=503)
        if not order.payment_reference:
            return Response({'detail': 'This order has no Paystack payment reference.'}, status=400)
        try:
            payment = fetch_paystack_transaction(order.payment_reference)
        except requests.RequestException:
            return Response({'detail': 'Paystack could not be reached. Try again shortly.'}, status=503)
        if not payment or payment.get('reference') != order.payment_reference:
            return Response({'detail': 'Paystack has not confirmed this payment reference as successful.'}, status=400)
        expected = to_pesewas(order.total_price)
        if payment.get('currency') != 'GHS' or payment.get('amount') != expected:
            logger.warning('Manual reconciliation amount mismatch for order %s', order.order_number)
            return Response({'detail': 'Paystack amount or currency does not match this order.'}, status=400)
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=pk)
            if locked.payment_status != 'paid':
                old = locked.payment_status
                locked.payment_status = 'paid'
                locked.save(update_fields=['payment_status', 'updated_at'])
                OrderEvent.objects.create(order=locked, actor=request.user, event_type='payment_reconciled', old_value=old, new_value='paid', note='Verified against Paystack from admin reconciliation.')
        return Response({'status': 'paid', 'order_id': pk, 'reference': order.payment_reference})


class AdminBulkVariantUpdateView(APIView):
    """Bulk price, old-price and availability updates for selected variants."""
    permission_classes = [permissions.IsAdminUser]

    def post(self, request):
        payload = request.data
        action = payload.get('action')
        ids = payload.get('variant_ids') or []
        if action in ('apply_discount', 'end_sale', 'set_availability'):
            if not isinstance(ids, list) or not ids:
                return Response({'detail': 'variant_ids must be a non-empty list.'}, status=400)
            try:
                percent = Decimal(str(payload.get('discount_percent', '0')))
            except Exception:
                return Response({'detail': 'discount_percent must be numeric.'}, status=400)
            if action == 'apply_discount' and not (Decimal('0') < percent < Decimal('100')):
                return Response({'detail': 'discount_percent must be greater than 0 and less than 100.'}, status=400)
            updated_ids = []
            phone_ids = set()
            with transaction.atomic():
                variants = list(StorageVariant.objects.select_for_update().filter(id__in=ids).select_related('phone'))
                if len(variants) != len(set(ids)):
                    return Response({'detail': 'One or more variants were not found.'}, status=404)
                for variant in variants:
                    if action == 'apply_discount':
                        base_price = variant.old_price if variant.old_price is not None and variant.old_price > variant.price else variant.price
                        variant.old_price = base_price
                        variant.price = (base_price * (Decimal('100') - percent) / Decimal('100')).quantize(Decimal('0.01'))
                    elif action == 'end_sale':
                        if variant.old_price is not None and variant.old_price > variant.price:
                            variant.price = variant.old_price
                        variant.old_price = None
                    elif action == 'set_availability':
                        variant.availability = bool(payload.get('availability'))
                    variant.save()
                    AdminAuditEvent.objects.create(actor=request.user, action='variant_bulk_action', object_type='StorageVariant', object_id=str(variant.id), details={'action': action, 'price': str(variant.price), 'old_price': str(variant.old_price) if variant.old_price is not None else None, 'availability': variant.availability})
                    updated_ids.append(variant.id)
                    phone_ids.add(variant.phone_id)
            for phone_id in phone_ids:
                invalidate_phone_caches(phone_id)
            return Response({'updated': len(updated_ids), 'variant_ids': updated_ids})

        updates = payload.get('updates')
        if not isinstance(updates, list) or not updates:
            return Response({'detail': 'Provide a non-empty updates array or a supported bulk action.'}, status=400)
        updated_ids, phone_ids = [], set()
        with transaction.atomic():
            for item in updates:
                if not isinstance(item, dict) or 'id' not in item:
                    return Response({'detail': 'Each update must contain a variant id.'}, status=400)
                variant = get_object_or_404(StorageVariant.objects.select_for_update().select_related('phone'), pk=item['id'])
                fields = {key: item[key] for key in ('price', 'old_price', 'availability') if key in item}
                if not fields:
                    continue
                serializer = StorageVariantWriteSerializer(variant, data=fields, partial=True, context={'request': request})
                serializer.is_valid(raise_exception=True)
                serializer.save()
                AdminAuditEvent.objects.create(actor=request.user, action='variant_updated', object_type='StorageVariant', object_id=str(variant.id), details={'changed_fields': fields})
                updated_ids.append(variant.id)
                phone_ids.add(variant.phone_id)
        for phone_id in phone_ids:
            invalidate_phone_caches(phone_id)
        return Response({'updated': len(updated_ids), 'variant_ids': updated_ids})


class AdminCustomerListView(APIView):
    """Customer roll-up grouped by normalized customer phone number."""
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        latest_name = Order.objects.filter(customer_phone_number=OuterRef('customer_phone_number')).order_by('-created_at').values('customer_name')[:1]
        latest_email = Order.objects.filter(customer_phone_number=OuterRef('customer_phone_number')).order_by('-created_at').values('customer_email')[:1]
        line_total = ExpressionWrapper(F('items__quantity') * F('items__price_at_purchase'), output_field=DecimalField(max_digits=12, decimal_places=2))
        customers = Order.objects.values('customer_phone_number').annotate(
            orders_count=Count('id', distinct=True),
            lifetime_spend=Sum(line_total, filter=(Q(payment_status='paid') | Q(payment_method='cash_on_delivery', cash_collected=True))),
            last_order=Max('created_at'),
            customer_name=Subquery(latest_name),
            customer_email=Subquery(latest_email),
        ).order_by('-last_order')
        search = (request.query_params.get('search') or '').strip()
        if search:
            customers = customers.filter(Q(customer_phone_number__icontains=search) | Q(customer_name__icontains=search) | Q(customer_email__icontains=search))
        try:
            page = max(int(request.query_params.get('page', 1) or 1), 1)
            page_size = min(max(int(request.query_params.get('page_size', 50) or 50), 1), 100)
        except (TypeError, ValueError):
            return Response({'detail': 'page and page_size must be integers.'}, status=400)
        total = customers.count()
        start = (page - 1) * page_size
        results = [{**row, 'lifetime_spend': str(row['lifetime_spend'] or 0), 'repeat_customer': row['orders_count'] > 1, 'last_order': row['last_order'].isoformat() if row['last_order'] else None} for row in customers[start:start + page_size]]
        return Response({'count': total, 'results': results, 'page': page, 'page_size': page_size})


class AdminDeliverySheetView(APIView):
    """Download a delivery CSV grouped/sorted by region and town."""
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        qs = Order.objects.exclude(status='cancelled').prefetch_related('items__phone', 'items__memory_variant').order_by('region', 'town', 'created_at')
        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        output = HttpResponse(content_type='text/csv; charset=utf-8')
        output['Content-Disposition'] = 'attachment; filename="itrade-delivery-sheet.csv"'
        writer = csv.writer(output)
        def safe_cell(value):
            value = '' if value is None else str(value)
            return "'" + value if value.startswith(('=', '+', '-', '@')) else value
        writer.writerow(['Region', 'Town', 'Order Number', 'Customer', 'Phone', 'Delivery Address', 'Items', 'Quantity', 'Total (GHS)', 'Payment Method', 'Payment Status', 'Order Status', 'Cash Collected', 'Order Date'])
        for order in qs:
            item_labels = [f"{item.quantity} x {item.phone.brand} {item.phone.model} {item.memory_variant.ram.ram}/{item.memory_variant.storage_capacity}" for item in order.items.all()]
            writer.writerow([safe_cell(order.region), safe_cell(order.town), safe_cell(order.order_number), safe_cell(order.customer_name), safe_cell(order.customer_phone_number), safe_cell(order.delivery_address), safe_cell('; '.join(item_labels)), order.total_quantity, order.total_price, safe_cell(order.payment_method), safe_cell(order.payment_status), safe_cell(order.status), 'Yes' if order.cash_collected else 'No', timezone.localtime(order.created_at).strftime('%Y-%m-%d %H:%M')])
        return output

class PaystackWebhook(APIView):
    """Paystack calls this server-to-server, so a payment is recorded even if
    the customer closes the tab before the frontend verifies it."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        # Read the raw body first, because the signature covers the exact bytes.
        signature = request.META.get('HTTP_X_PAYSTACK_SIGNATURE', '')
        expected = hmac.new(
            (getattr(settings, 'PAYSTACK_SECRET_KEY', None) or getattr(settings, 'PAYSTACK_SECRET_TEST', '')).encode(), request.body, hashlib.sha512
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        event = request.data
        if event.get('event') == 'charge.success':
            data = event.get('data') or {}
            order = Order.objects.filter(payment_reference=data.get('reference')).first()
            if order:
                if data.get('currency') == 'GHS' and data.get('amount', 0) == to_pesewas(order.total_price):
                    with transaction.atomic():
                        locked = Order.objects.select_for_update().get(pk=order.pk)
                        if locked.payment_status != 'paid':
                            old_status = locked.payment_status
                            locked.payment_status = 'paid'
                            locked.save(update_fields=['payment_status', 'updated_at'])
                            OrderEvent.objects.create(order=locked, event_type='payment_reconciled', old_value=old_status, new_value='paid', note='Confirmed by Paystack webhook.')
                else:
                    logger.warning("Webhook amount mismatch for order %s", order.order_number)

        return Response(status=status.HTTP_200_OK)  # always 200 so Paystack doesn't retry


class PublicStoreStatusView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        obj = StoreSetting.objects.filter(pk=1).first()
        if not obj:
            return Response({'orders_paused': False, 'announcement': '', 'whatsapp_number': ''})
        return Response({'orders_paused': obj.orders_paused, 'announcement': obj.announcement, 'whatsapp_number': obj.whatsapp_number})


# ==========================================================================
# PHASE 3: FUNNEL, DEMAND, INBOX REPLIES, SECURITY, STORE CONTROLS, SEARCH
# ==========================================================================

class AdminFunnelView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        days = min(max(int(request.query_params.get('days', 30) or 30), 1), 180)
        start = timezone.now() - timedelta(days=days)
        visits = Visit.objects.filter(is_bot=False, timestamp__gte=start)
        events = ['page_view', 'product_view', 'add_to_cart', 'checkout_start', 'purchase']
        counts = {row['event']: row['total'] for row in visits.values('event').annotate(total=Count('id'))}
        unique_sessions = {row['event']: row['total'] for row in visits.exclude(session_id__isnull=True).exclude(session_id='').values('event').annotate(total=Count('session_id', distinct=True))}
        funnel = [{'event': e, 'events': counts.get(e, 0), 'unique_visitors': unique_sessions.get(e, 0), 'unique_sessions': unique_sessions.get(e, 0)} for e in events]
        sources = list(visits.exclude(utm_source='').values('utm_source', 'utm_medium', 'utm_campaign').annotate(visits=Count('id')).order_by('-visits')[:12])
        referrers = list(visits.exclude(referrer__isnull=True).exclude(referrer='').values('referrer').annotate(visits=Count('id')).order_by('-visits')[:10])
        return Response({'days': days, 'funnel': funnel, 'sources': sources, 'referrers': referrers})


class AdminDemandSignalsView(APIView):
    """Product interest signals based on anonymous browsing and guest orders.

    This endpoint deliberately does not read or calculate stock quantities.
    Product IDs are phone IDs supplied by the storefront event tracker.
    """
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        try:
            days = min(max(int(request.query_params.get('days', 30) or 30), 1), 180)
        except (TypeError, ValueError):
            return Response({'detail': 'days must be an integer.'}, status=400)

        start = timezone.now() - timedelta(days=days)
        tracked_events = Visit.objects.filter(
            is_bot=False,
            timestamp__gte=start,
            product_id__isnull=False,
            event__in=['product_view', 'add_to_cart'],
        )
        event_rows = tracked_events.values('product_id', 'event').annotate(
            event_count=Count('id'),
            unique_visitors=Count('session_id', distinct=True),
        )
        metrics = {}
        for row in event_rows:
            item = metrics.setdefault(row['product_id'], {
                'views': 0, 'view_visitors': 0,
                'cart_adds': 0, 'cart_visitors': 0,
            })
            if row['event'] == 'product_view':
                item['views'] = row['event_count']
                item['view_visitors'] = row['unique_visitors']
            else:
                item['cart_adds'] = row['event_count']
                item['cart_visitors'] = row['unique_visitors']

        phones = {
            phone.id: phone
            for phone in Phone.objects.filter(id__in=list(metrics.keys()))
        }
        order_rows = OrderItem.objects.filter(
            order__created_at__gte=start,
        ).exclude(
            order__status='cancelled',
        ).values('phone_id').annotate(
            ordered_quantity=Sum('quantity'),
            orders=Count('order_id', distinct=True),
        )
        orders_by_phone = {
            row['phone_id']: {
                'ordered_quantity': int(row['ordered_quantity'] or 0),
                'orders': int(row['orders'] or 0),
            }
            for row in order_rows
        }

        signals = []
        for phone_id, counts in metrics.items():
            phone = phones.get(phone_id)
            if not phone:
                continue
            order_data = orders_by_phone.get(
                phone_id, {'ordered_quantity': 0, 'orders': 0}
            )
            views = counts['views']
            cart_adds = counts['cart_adds']
            orders = order_data['orders']

            if cart_adds >= 3 and orders == 0:
                signal = 'cart_interest_no_orders'
            elif views >= 20 and orders == 0:
                signal = 'high_interest_no_sales'
            elif orders > 0 and (views >= 10 or cart_adds >= 2):
                signal = 'converting_interest'
            else:
                signal = 'normal'

            signals.append({
                'phone_id': phone.id,
                'product': str(phone),
                'views': views,
                'unique_visitors': max(
                    counts['view_visitors'], counts['cart_visitors']
                ),
                'cart_adds': cart_adds,
                'cart_visitors': counts['cart_visitors'],
                'orders': orders,
                'ordered_quantity': order_data['ordered_quantity'],
                'signal': signal,
            })

        signals.sort(
            key=lambda row: (row['cart_adds'], row['views']), reverse=True
        )
        return Response({'days': days, 'product_signals': signals[:50]})


class AdminAnalyticsOverviewView(APIView):
    """One aggregated business-intelligence response for the admin Analytics page.

    Revenue means collected revenue: paid online orders plus COD orders explicitly
    marked cash_collected. Customer identity is the guest checkout phone number.
    """
    permission_classes = [permissions.IsAdminUser]
    CACHE_TTL = 30

    @staticmethod
    def _unique_visitors(queryset):
        sessions = queryset.exclude(
            Q(session_id__isnull=True) | Q(session_id='')
        ).values('session_id').distinct().count()
        fallback_ips = queryset.filter(
            Q(session_id__isnull=True) | Q(session_id='')
        ).exclude(
            Q(ip_address__isnull=True) | Q(ip_address='')
        ).values('ip_address').distinct().count()
        return sessions + fallback_ips

    def get(self, request):
        range_key = request.query_params.get('range', '30d')
        range_days = {'7d': 7, '30d': 30, '90d': 90, '12m': 365}
        days = range_days.get(range_key, 30)
        cache_key = f'admin:analytics:overview:v1:{days}'
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        now = timezone.now()
        start = now - timedelta(days=days - 1)
        previous_start = start - timedelta(days=days)
        current_orders = Order.objects.filter(created_at__gte=start, created_at__lt=now)
        previous_orders = Order.objects.filter(
            created_at__gte=previous_start, created_at__lt=start
        )
        collected_q = (
            ~Q(order__status='cancelled') & (
                Q(order__payment_status='paid') |
                Q(order__payment_method='cash_on_delivery', order__cash_collected=True)
            )
        )
        collected_order_q = (
            ~Q(status='cancelled') & (
                Q(payment_status='paid') |
                Q(payment_method='cash_on_delivery', cash_collected=True)
            )
        )
        line_total = ExpressionWrapper(
            F('price_at_purchase') * F('quantity'),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        )

        def collected_revenue(order_queryset):
            return OrderItem.objects.filter(order__in=order_queryset).filter(
                collected_q
            ).aggregate(total=Sum(line_total))['total'] or Decimal('0')

        revenue = collected_revenue(current_orders)
        previous_revenue = collected_revenue(previous_orders)
        orders_count = current_orders.count()
        previous_orders_count = previous_orders.count()
        collected_order_count = current_orders.filter(collected_order_q).count()
        aov = revenue / collected_order_count if collected_order_count else Decimal('0')

        # Daily order and collected-revenue trends.
        order_daily_rows = current_orders.annotate(day=TruncDate('created_at')).values('day').annotate(
            orders=Count('id')
        )
        orders_by_day = {row['day']: row['orders'] for row in order_daily_rows}
        revenue_daily_rows = OrderItem.objects.filter(
            order__created_at__gte=start, order__created_at__lt=now
        ).filter(collected_q).annotate(day=TruncDate('order__created_at')).values('day').annotate(
            revenue=Sum(line_total)
        )
        revenue_by_day = {row['day']: row['revenue'] or Decimal('0') for row in revenue_daily_rows}

        # Visit/event analytics. Bot traffic is excluded throughout.
        visits = Visit.objects.filter(is_bot=False, timestamp__gte=start, timestamp__lt=now)
        previous_visits = Visit.objects.filter(
            is_bot=False, timestamp__gte=previous_start, timestamp__lt=start
        )
        visitors = self._unique_visitors(visits)
        previous_visitors = self._unique_visitors(previous_visits)
        page_visits = visits.filter(event='page_view')
        page_views = page_visits.count()
        visitor_daily_rows = visits.exclude(
            Q(session_id__isnull=True) | Q(session_id='')
        ).annotate(day=TruncDate('timestamp')).values('day').annotate(
            visitors=Count('session_id', distinct=True)
        )
        visitor_by_day = {row['day']: row['visitors'] for row in visitor_daily_rows}
        fallback_daily_rows = visits.filter(
            Q(session_id__isnull=True) | Q(session_id='')
        ).exclude(Q(ip_address__isnull=True) | Q(ip_address='')).annotate(
            day=TruncDate('timestamp')
        ).values('day').annotate(visitors=Count('ip_address', distinct=True))
        for row in fallback_daily_rows:
            visitor_by_day[row['day']] = visitor_by_day.get(row['day'], 0) + row['visitors']

        event_names = ['page_view', 'product_view', 'add_to_cart', 'checkout_start', 'purchase']
        event_rows = visits.values('event').annotate(events=Count('id'), visitors=Count('session_id', distinct=True))
        event_map = {row['event']: row for row in event_rows}
        funnel = [{
            'event': event,
            'events': event_map.get(event, {}).get('events', 0),
            'unique_visitors': event_map.get(event, {}).get('visitors', 0),
        } for event in event_names]
        purchase_visitors = event_map.get('purchase', {}).get('visitors', 0)
        conversion_rate = round((purchase_visitors / visitors) * 100, 2) if visitors else 0

        # Guest customers: the phone number is the stable business-level identifier.
        active_customer_rows = current_orders.exclude(customer_phone_number='').values(
            'customer_phone_number'
        ).annotate(order_count=Count('id'))
        active_phones = {row['customer_phone_number'] for row in active_customer_rows}
        prior_phones = set(Order.objects.filter(
            created_at__lt=start
        ).exclude(customer_phone_number='').values_list('customer_phone_number', flat=True).distinct())
        repeat_phones = {
            row['customer_phone_number'] for row in active_customer_rows
            if row['customer_phone_number'] in prior_phones or row['order_count'] > 1
        }
        active_customers = len(active_phones)
        repeat_customers = len(repeat_phones)
        new_customers = len(active_phones - prior_phones)
        all_customers = Order.objects.exclude(customer_phone_number='').values(
            'customer_phone_number'
        ).distinct().count()
        repeat_rate = round((repeat_customers / active_customers) * 100, 1) if active_customers else 0

        # Product performance uses collected order value and does not use stock fields.
        top_products = list(OrderItem.objects.filter(
            order__created_at__gte=start, order__created_at__lt=now
        ).filter(collected_q).values(
            'phone_id', 'phone__brand', 'phone__model'
        ).annotate(
            units=Sum('quantity'),
            revenue=Sum(line_total),
            orders=Count('order_id', distinct=True),
        ).order_by('-revenue')[:10])
        top_products = [{
            'phone_id': row['phone_id'],
            'product': f"{row['phone__brand']} {row['phone__model']}",
            'units': int(row['units'] or 0),
            'orders': int(row['orders'] or 0),
            'revenue': str(row['revenue'] or Decimal('0')),
        } for row in top_products]

        top_customers = list(OrderItem.objects.filter(
            order__created_at__gte=start, order__created_at__lt=now
        ).filter(collected_q).values(
            'order__customer_name', 'order__customer_phone_number'
        ).annotate(
            revenue=Sum(line_total),
            orders=Count('order_id', distinct=True),
        ).order_by('-revenue')[:10])
        top_customers = [{
            'name': row['order__customer_name'],
            'phone': row['order__customer_phone_number'],
            'orders': int(row['orders'] or 0),
            'revenue': str(row['revenue'] or Decimal('0')),
        } for row in top_customers]

        payment_split = list(current_orders.values('payment_method').annotate(
            orders=Count('id')
        ).order_by('-orders'))
        order_statuses = list(current_orders.values('status').annotate(
            orders=Count('id')
        ).order_by('-orders'))
        regions = list(current_orders.values('region').annotate(
            orders=Count('id')
        ).order_by('-orders')[:10])
        sources = list(page_visits.exclude(utm_source='').values(
            'utm_source', 'utm_medium', 'utm_campaign'
        ).annotate(visits=Count('id'), visitors=Count('session_id', distinct=True)).order_by('-visits')[:10])
        referrers = list(page_visits.exclude(referrer__isnull=True).exclude(referrer='').values(
            'referrer'
        ).annotate(visits=Count('id')).order_by('-visits')[:10])

        # Product interest / demand: tracked product views and cart additions versus guest orders.
        product_event_rows = visits.filter(
            product_id__isnull=False, event__in=['product_view', 'add_to_cart']
        ).values('product_id', 'event').annotate(
            events=Count('id'), visitors=Count('session_id', distinct=True)
        )
        interest = {}
        for row in product_event_rows:
            item = interest.setdefault(row['product_id'], {
                'views': 0, 'view_visitors': 0, 'cart_adds': 0, 'cart_visitors': 0
            })
            if row['event'] == 'product_view':
                item['views'] = row['events']
                item['view_visitors'] = row['visitors']
            else:
                item['cart_adds'] = row['events']
                item['cart_visitors'] = row['visitors']
        demand_order_rows = OrderItem.objects.filter(
            order__created_at__gte=start, order__created_at__lt=now
        ).exclude(order__status='cancelled').values('phone_id').annotate(
            orders=Count('order_id', distinct=True), units=Sum('quantity')
        )
        demand_orders = {row['phone_id']: row for row in demand_order_rows}
        phone_map = {p.id: p for p in Phone.objects.filter(id__in=list(interest.keys()))}
        demand_signals = []
        for phone_id, metrics in interest.items():
            phone = phone_map.get(phone_id)
            if not phone:
                continue
            sold = demand_orders.get(phone_id, {})
            orders_for_phone = int(sold.get('orders') or 0)
            if metrics['cart_adds'] >= 3 and orders_for_phone == 0:
                signal = 'Cart interest, no orders'
            elif metrics['views'] >= 20 and orders_for_phone == 0:
                signal = 'High views, no orders'
            elif orders_for_phone > 0 and (metrics['views'] >= 10 or metrics['cart_adds'] >= 2):
                signal = 'Interest converting'
            else:
                signal = 'Emerging interest'
            demand_signals.append({
                'phone_id': phone_id,
                'product': str(phone),
                'views': metrics['views'],
                'unique_visitors': max(metrics['view_visitors'], metrics['cart_visitors']),
                'cart_adds': metrics['cart_adds'],
                'orders': orders_for_phone,
                'units': int(sold.get('units') or 0),
                'signal': signal,
            })
        demand_signals.sort(key=lambda item: (item['cart_adds'], item['views']), reverse=True)

        cancelled_orders = current_orders.filter(status='cancelled').count()
        cancellation_rate = round((cancelled_orders / orders_count) * 100, 1) if orders_count else 0
        messages = ContactMessage.objects.filter(created_at__gte=start, created_at__lt=now)
        message_stats = {
            'total': messages.count(),
            'new': messages.filter(status='new').count(),
            'replied': messages.filter(status='replied').count(),
        }

        daily = []
        first_day = now.date() - timedelta(days=days - 1)
        for offset in range(days):
            day = first_day + timedelta(days=offset)
            daily.append({
                'date': day.isoformat(),
                'orders': int(orders_by_day.get(day, 0)),
                'revenue': str(revenue_by_day.get(day, Decimal('0'))),
                'visitors': int(visitor_by_day.get(day, 0)),
            })

        def pct_change(current_value, previous_value):
            if not previous_value:
                return 100 if current_value else 0
            return round((float(current_value - previous_value) / float(previous_value)) * 100, 1)

        data = {
            'range': range_key,
            'days': days,
            'generated_at': now.isoformat(),
            'kpis': {
                'collected_revenue': str(revenue),
                'revenue_change_percent': pct_change(revenue, previous_revenue),
                'orders': orders_count,
                'orders_change_percent': pct_change(orders_count, previous_orders_count),
                'average_order_value': str(aov),
                'visitors': visitors,
                'visitors_change_percent': pct_change(visitors, previous_visitors),
                'page_views': page_views,
                'conversion_rate': conversion_rate,
                'revenue_per_visitor': str((revenue / visitors) if visitors else Decimal('0')),
                'orders_per_customer': round((orders_count / active_customers), 2) if active_customers else 0,
                'active_customers': active_customers,
                'all_customers': all_customers,
                'new_customers': new_customers,
                'repeat_customers': repeat_customers,
                'repeat_rate': repeat_rate,
                'cancellation_rate': cancellation_rate,
                'cancelled_orders': cancelled_orders,
                'collected_orders': collected_order_count,
            },
            'daily': daily,
            'funnel': funnel,
            'payment_split': payment_split,
            'order_statuses': order_statuses,
            'regions': regions,
            'top_products': top_products,
            'top_customers': top_customers,
            'sources': sources,
            'referrers': referrers,
            'demand_signals': demand_signals[:12],
            'messages': message_stats,
            'customers': {
                'active': active_customers,
                'all_time': all_customers,
                'new': new_customers,
                'repeat': repeat_customers,
                'repeat_rate': repeat_rate,
            },
        }
        cache.set(cache_key, data, self.CACHE_TTL)
        return Response(data)

class AdminStoreControlsView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        obj = StoreSetting.current()
        return Response({'orders_paused': obj.orders_paused, 'announcement': obj.announcement, 'whatsapp_number': obj.whatsapp_number, 'updated_at': obj.updated_at})

    def patch(self, request):
        obj = StoreSetting.current()
        before = {'orders_paused': obj.orders_paused, 'announcement': obj.announcement, 'whatsapp_number': obj.whatsapp_number}
        for field in ('orders_paused', 'announcement', 'whatsapp_number'):
            if field in request.data:
                value = request.data[field]
                if field == 'orders_paused':
                    if not isinstance(value, bool):
                        return Response({'detail': 'orders_paused must be true or false.'}, status=400)
                else:
                    value = str(value).strip()
                    if len(value) > (500 if field == 'announcement' else 30):
                        return Response({'detail': f'{field} is too long.'}, status=400)
                setattr(obj, field, value)
        obj.updated_by = request.user
        obj.save()
        after = {'orders_paused': obj.orders_paused, 'announcement': obj.announcement, 'whatsapp_number': obj.whatsapp_number}
        AdminAuditEvent.objects.create(actor=request.user, action='store_settings_updated', object_type='StoreSetting', object_id='1', details={'before': before, 'after': after})
        cache.delete('admin:counts:v2')
        return Response(after)


class AdminMessageReplyView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def post(self, request, pk):
        message = get_object_or_404(ContactMessage, pk=pk)
        body = str(request.data.get('body') or '').strip()
        subject = str(request.data.get('subject') or f'Re: {message.subject}').strip()[:255]
        if not body:
            return Response({'detail': 'Reply body is required.'}, status=400)
        api_key = os.getenv('RESEND_API_KEY')
        from_email = os.getenv('RESEND_FROM_EMAIL')
        if not api_key or not from_email:
            return Response({'detail': 'Email replies are not configured. Set RESEND_API_KEY and RESEND_FROM_EMAIL.'}, status=503)
        try:
            response = requests.post('https://api.resend.com/emails', headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'from': from_email, 'to': [message.email], 'subject': subject, 'text': body, 'reply_to': from_email}, timeout=15)
            response.raise_for_status()
            provider_id = response.json().get('id', '')
        except requests.RequestException as exc:
            logger.exception('Resend email delivery failed')
            return Response({'detail': 'Email provider could not send the reply.'}, status=502)
        MessageReply.objects.create(message=message, admin=request.user, subject=subject, body=body, provider_id=provider_id)
        if message.status != 'replied':
            message.status = 'replied'
            message.save(update_fields=['status'])
        AdminAuditEvent.objects.create(actor=request.user, action='message_replied', object_type='ContactMessage', object_id=str(message.pk), details={'recipient': message.email, 'provider_id': provider_id})
        return Response({'status': 'sent', 'provider_id': provider_id}, status=201)


class AdminSecurityEventsView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        events = SecurityEvent.objects.order_by('-created_at')[:100]
        return Response([{'id': e.id, 'kind': e.kind, 'ip_hash': e.ip_hash[:12] + '…', 'path': e.path, 'severity': e.severity, 'seen': e.seen, 'created_at': e.created_at} for e in events])


class AdminAuditEventsView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        admin_rows = list(AdminAuditEvent.objects.select_related('actor')[:100])
        order_rows = list(OrderEvent.objects.select_related('actor', 'order')[:100])
        combined = [
            {'id': f'admin-{e.id}', 'actor': e.actor.username if e.actor else 'System', 'action': e.action, 'object_type': e.object_type, 'object_id': e.object_id, 'details': e.details, 'created_at': e.created_at}
            for e in admin_rows
        ] + [
            {'id': f'order-{e.id}', 'actor': e.actor.username if e.actor else 'System', 'action': e.event_type, 'object_type': 'Order', 'object_id': e.order.order_number, 'details': {'old_value': e.old_value, 'new_value': e.new_value, 'note': e.note}, 'created_at': e.created_at}
            for e in order_rows
        ]
        combined.sort(key=lambda item: item['created_at'], reverse=True)
        return Response(combined[:100])


class AdminGlobalSearchView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        q = (request.query_params.get('q') or '').strip()[:100]
        if len(q) < 2:
            return Response({'orders': [], 'customers': [], 'messages': [], 'phones': []})
        orders = Order.objects.filter(Q(order_number__icontains=q) | Q(customer_name__icontains=q) | Q(customer_phone_number__icontains=q) | Q(customer_email__icontains=q)).order_by('-created_at')[:6]
        messages = ContactMessage.objects.filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(subject__icontains=q)).order_by('-created_at')[:6]
        phones = Phone.objects.filter(Q(brand__icontains=q) | Q(model__icontains=q))[:6]
        customers = Order.objects.filter(Q(customer_name__icontains=q) | Q(customer_phone_number__icontains=q) | Q(customer_email__icontains=q)).values('customer_name', 'customer_phone_number', 'customer_email').distinct()[:6]
        return Response({'orders': [{'id': o.id, 'order_number': o.order_number, 'customer_name': o.customer_name, 'status': o.status} for o in orders], 'customers': list(customers), 'messages': [{'id': m.id, 'name': m.name, 'subject': m.subject, 'status': m.status} for m in messages], 'phones': [{'id': p.id, 'name': str(p)} for p in phones]})
