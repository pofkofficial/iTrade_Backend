# store/serializers.py
from django.utils import timezone
from datetime import timedelta
from rest_framework import serializers
from .models import Phone, Order, ContactMessage, StorageVariant, RAMVariant, OrderItem
from django.core.validators import MinValueValidator
from django.db import transaction
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)

# ============================================================================
# OPTIMIZED SERIALIZERS FOR LIST VIEWS (Lightweight)
# ============================================================================

class RAMVariantListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for RAM variants in list views"""
    class Meta:
        model = RAMVariant
        fields = ['id', 'ram']


class StorageVariantListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for storage variants in list views"""
    ram = serializers.CharField(source='ram.ram', read_only=True)

    class Meta:
        model = StorageVariant
        fields = ['id', 'storage_capacity', 'stock', 'price', 'old_price', 'ram']


class PhoneListSerializer(serializers.ModelSerializer):
    """Optimized serializer for phone list views with minimal data"""
    min_price = serializers.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        read_only=True,
        source='get_min_price'
    )
    max_price = serializers.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        read_only=True,
        source='get_max_price'
    )
    storage_options = serializers.ListField(
        child=serializers.DictField(),  # Fastest approach
        read_only=True,
        source='get_storage_options'
    )
    has_discount = serializers.BooleanField(read_only=True, source='has_discount_variants')

    class Meta:
        model = Phone
        fields = [
            'id', 'brand', 'model', 'thumbnail', 'min_price', 'max_price',
            'storage_options', 'has_discount' 
        ]
        read_only_fields = fields


# ============================================================================
# DETAIL SERIALIZERS (Full data)
# ============================================================================

class RAMVariantDetailSerializer(serializers.ModelSerializer):
    """
    Unified serializer for RAMVariant:
    - List / detail: returns id, ram, phone
    - Create: accepts phone (as PK) + ram, with duplicate validation
    """
    phone = serializers.PrimaryKeyRelatedField(
        queryset=Phone.objects.all(),
        write_only=True,               # ← important: hide full phone object on output
    )
    phone_info = serializers.StringRelatedField(
        source='phone',
        read_only=True,
        label="Phone"
    )  # optional: nicer output like "Samsung Galaxy S23"

    class Meta:
        model = RAMVariant
        fields = [
            'id',
            'ram',
            'phone',         # write-only (PK)
            'phone_info',    # read-only (human-friendly)
        ]
        read_only_fields = ['id', 'phone_info']

    def validate(self, data):
        """
        Prevent duplicate RAM for the same phone
        """
        phone = data.get('phone')
        ram_value = data.get('ram')

        if phone and ram_value:
            if RAMVariant.objects.filter(phone=phone, ram=ram_value).exists():
                raise serializers.ValidationError(
                    f"RAM variant '{ram_value}' already exists for this phone."
                )
        return data

class StorageVariantDetailSerializer(serializers.ModelSerializer):
    """Full serializer for storage variant details"""
    ram = RAMVariantDetailSerializer(read_only=True)

    class Meta:
        model = StorageVariant
        fields = ['id', 'storage_capacity', 'stock', 'price', 'old_price', 'ram', 'phone']
        read_only_fields = fields


class PhoneDetailSerializer(serializers.ModelSerializer):
    """Full serializer for phone detail views"""

    class Meta:
        model = Phone
        fields = [
            'battery_capacity', 'display_size', 'camera_details', 
            'Fingerprint', 'FaceScanner', 'Iris_scanner', 'physical_sim',
            'esim', 'number_of_sims', 'support_5G','photo_1', 'photo_2', 'photo_3', 'link'
        ]
        read_only_fields = fields


class StorageVariantWriteSerializer(serializers.ModelSerializer):
    """
    Writable serializer for creating/updating StorageVariant.
    - Required: phone, ram (for create)
    - Writable: price, old_price, stock, storage_capacity (optional for update)
    """
    phone = serializers.PrimaryKeyRelatedField(
        queryset=Phone.objects.all(),
        required=False,           # required only on create
        write_only=True
    )
    ram = serializers.PrimaryKeyRelatedField(
        queryset=RAMVariant.objects.all(),
        required=False,
        write_only=True
    )

    class Meta:
        model = StorageVariant
        fields = [
            'id', 'phone', 'ram', 'storage_capacity',
            'stock', 'price', 'old_price',
        ]
        read_only_fields = ['id']

    def validate(self, data):
        # For create (POST): require phone and ram
        if self.context['request'].method == 'POST':
            if not data.get('phone'):
                raise serializers.ValidationError({'phone': 'This field is required when creating a variant'})
            if not data.get('ram'):
                raise serializers.ValidationError({'ram': 'This field is required when creating a variant'})

            # Ensure ram belongs to the phone
            phone = data['phone']
            ram = data['ram']
            if ram.phone != phone:
                raise serializers.ValidationError({
                    'ram': 'Selected RAM variant must belong to the chosen phone'
                })

        # Common validation
        price = data.get('price')
        old_price = data.get('old_price')
        stock = data.get('stock')

        if price is not None and price < Decimal('0.01'):
            raise serializers.ValidationError({'price': 'Price must be at least 0.01'})
        if old_price is not None and old_price < Decimal('0.01'):
            raise serializers.ValidationError({'old_price': 'Old price must be at least 0.01'})
        if stock is not None and stock < 0:
            raise serializers.ValidationError({'stock': 'Stock cannot be negative'})

        return data

    def create(self, validated_data):
        # Create new variant
        return StorageVariant.objects.create(**validated_data)

    def update(self, instance, validated_data):
        # Update only provided fields (PATCH-friendly)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance

class PhoneUpdateSerializer(serializers.ModelSerializer):
    """Writable serializer for updating phone details (including images)"""
    class Meta:
        model = Phone
        fields = [
            'brand', 'model', 'display_size', 'camera_details', 'battery_capacity',
            'physical_sim', 'esim', 'number_of_sims', 'support_5G',
            'Fingerprint', 'FaceScanner', 'Iris_scanner', 'link',
            'thumbnail', 'photo_1', 'photo_2', 'photo_3',
        ]
        # No read_only_fields — all are writable here


class OrderItemWriteSerializer(serializers.ModelSerializer):
    """Serializer for writing order items (optimized for creation)"""
    phone_id = serializers.PrimaryKeyRelatedField(
        queryset=Phone.objects.all(),
        source='phone',
        write_only=True,
        required=True
    )
    memory_variant_id = serializers.PrimaryKeyRelatedField(
        queryset=StorageVariant.objects.select_related('phone', 'ram'),
        source='memory_variant',
        write_only=True,
        required=True
    )
    quantity = serializers.IntegerField(
        required=True,
        validators=[MinValueValidator(1)],
        min_value=1,
        max_value=100
    )
    price_at_purchase = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=True,
        validators=[MinValueValidator(0.01)]
    )

    class Meta:
        model = OrderItem
        fields = ['phone_id', 'memory_variant_id', 'quantity', 'price_at_purchase']
        error_messages = {
            'phone_id': {'does_not_exist': 'Phone not found'},
            'memory_variant_id': {'does_not_exist': 'Invalid memory variant'}
        }

    def validate(self, data):
        """Validate that memory variant belongs to the selected phone"""
        phone = data.get('phone')
        memory_variant = data.get('memory_variant')
        price_at_purchase = data.get('price_at_purchase')

        # Validate memory variant belongs to phone
        if memory_variant.phone != phone:
            logger.error(f"Validation failed: Memory variant {memory_variant.id} does not belong to phone {phone.id}")
            raise serializers.ValidationError({
                'memory_variant_id': 'Memory variant does not belong to the selected phone'
            })
        
        # Validate price consistency (optional but recommended)
        if memory_variant.price != price_at_purchase:
            logger.error(f"Validation failed: Price mismatch for memory variant {memory_variant.id}: expected {memory_variant.price}, received {price_at_purchase}")
            raise serializers.ValidationError({
                'price_at_purchase': f"Price mismatch: expected {memory_variant.price}, received {price_at_purchase}"
            })
        
        return data


class OrderWriteSerializer(serializers.ModelSerializer):
    """Serializer for creating orders (optimized for performance)"""
    items = OrderItemWriteSerializer(many=True, required=True)

    class Meta:
        model = Order
        fields = [
            'customer_name', 'customer_email', 'customer_phone_number',
            'street_address', 'town', 'region', 'postal_code',
            'payment_method', 'items'
        ]
        extra_kwargs = {
            'payment_method': {
                'error_messages': {
                    'invalid_choice': 'Invalid payment method'
                }
            }
        }

    def validate(self, data):
        """Validate order data"""
        items = data.get('items', [])
        if not items:
            logger.error("Validation failed: Order must contain at least one item")
            raise serializers.ValidationError({
                'items': 'Order must contain at least one item'
            })

        # Normalize payment method
        payment_method = data.get('payment_method', '').lower().replace(' ', '_')
        if payment_method not in dict(Order.PAYMENT_METHODS):
            logger.error(f"Validation failed: Invalid payment method {payment_method}")
            raise serializers.ValidationError({
                'payment_method': 'Invalid payment method'
            })
        data['payment_method'] = payment_method

        # Validate cash on delivery region restriction
        if payment_method == 'cash_on_delivery' and data.get('region') != 'Greater Accra':
            logger.error("Validation failed: Cash on delivery is only available for Greater Accra region")
            raise serializers.ValidationError({
                'payment_method': 'Cash on delivery is only available for Greater Accra region'
            })

        return data

    def create(self, validated_data):
        """Create order with transaction atomicity"""
        with transaction.atomic():
            items_data = validated_data.pop('items')
            # Set defaults
            validated_data.setdefault('status', 'pending')
            validated_data.setdefault('estimated_delivery', timezone.now() + timedelta(days=5))
            
            # Create order
            order = Order.objects.create(**validated_data)
            
            # Create order items in bulk
            order_items = [
                OrderItem(
                    order=order,
                    phone=item_data['phone'],
                    memory_variant=item_data['memory_variant'],
                    quantity=item_data['quantity'],
                    price_at_purchase=item_data['price_at_purchase']
                ) for item_data in items_data
            ]
            OrderItem.objects.bulk_create(order_items)
            
            # REMOVED: Stock update logic completely
            # No stock reduction happens here anymore
            
        return order

class OrderItemReadSerializer(serializers.ModelSerializer):
    """Serializer for reading order items (includes nested relationships)"""
    phone = PhoneListSerializer(read_only=True)
    memory_variant = StorageVariantListSerializer(read_only=True)
    line_total = serializers.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        read_only=True,
        source='total_price'
    )

    class Meta:
        model = OrderItem
        fields = [
            'phone', 'memory_variant', 'quantity', 'price_at_purchase', 'line_total'
        ]
        read_only_fields = fields

class OrderReadSerializer(serializers.ModelSerializer):
    """Serializer for reading orders (includes calculated fields and nested data)"""
    items = OrderItemReadSerializer(many=True, read_only=True)
    total_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    delivery_address = serializers.CharField(read_only=True)
    total_quantity = serializers.IntegerField(read_only=True)
    status_display = serializers.CharField(read_only=True, source='get_status_display')
    payment_method_display = serializers.CharField(read_only=True, source='get_payment_method_display')

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'status', 'status_display', 'created_at', 'updated_at',
            'customer_name', 'customer_email', 'customer_phone_number',
            'street_address', 'town', 'region', 'postal_code', 'delivery_address',
            'payment_method', 'payment_method_display', 'payment_status', 'payment_reference',
            'items', 'total_price', 'total_quantity', 'estimated_delivery'
        ]
        read_only_fields = fields

    def get_total_price(self, obj):
        return obj.total_price
    
    def get_total_quantity(self, obj):
        return obj.total_quantity
    
class OrderUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating order status"""
    class Meta:
        model = Order
        fields = ['status']
        extra_kwargs = {
            'status': {
                'error_messages': {
                    'invalid_choice': 'Status must be either pending, shipped, delivered, or cancelled'
                }
            }
        }


# ============================================================================
# OTHER SERIALIZERS
# ============================================================================

class ContactMessageSerializer(serializers.ModelSerializer):
    """Serializer for contact messages"""
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = ContactMessage
        fields = [
            'id', 'name', 'email', 'phone', 'subject', 'message', 
            'created_at', 'status', 'status_display'
        ]
        read_only_fields = ('created_at', 'status')


class ContactMessageCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating contact messages (no read-only fields)"""
    class Meta:
        model = ContactMessage
        fields = ['name', 'email', 'phone', 'subject', 'message']


class ContactMessageStatusSerializer(serializers.ModelSerializer):
    """Serializer for updating contact message status"""
    class Meta:
        model = ContactMessage
        fields = ['status']
        
        extra_kwargs = {
            'status': {
                'error_messages': {
                    'invalid_choice': 'Invalid status value'
                }
            }
        }


class UserLoginSerializer(serializers.Serializer):
    """Serializer for user authentication"""
    username = serializers.CharField(required=True)
    password = serializers.CharField(write_only=True, required=True)

    def validate(self, data):
        """Validate user credentials"""
        username = data.get('username')
        password = data.get('password')

        if not username or not password:
            raise serializers.ValidationError("Username and password are required")

        user = authenticate(username=username, password=password)
        if not user:
            raise serializers.ValidationError("Invalid credentials")
        
        if not user.is_active:
            raise serializers.ValidationError("Account is inactive")
        
        if not user.is_superuser:
            raise serializers.ValidationError("Admin privileges required")

        data['user'] = user
        return data


# ============================================================================
# LEGACY SERIALIZERS (For backward compatibility)
# ============================================================================

class PhoneSerializer(PhoneDetailSerializer):
    """Legacy serializer - alias for PhoneDetailSerializer"""
    pass


class StorageVariantSerializer(StorageVariantDetailSerializer):
    """Legacy serializer - alias for StorageVariantDetailSerializer"""
    pass


class RAMVariantSerializer(RAMVariantDetailSerializer):
    """Legacy serializer - alias for RAMVariantDetailSerializer"""
    pass