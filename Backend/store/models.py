# store/models.py
import uuid
from django.db import models
from django.forms import ValidationError
from django.utils import timezone
import hashlib
from django.core.validators import MinValueValidator
from django.db.models import Min, Max
from django.db import transaction

# Choices
STORAGE_CHOICES = [
    ('16GB', '16GB'),
    ('32GB', '32GB'),
    ('64GB', '64GB'),
    ('128GB', '128GB'),
    ('256GB', '256GB'),
    ('512GB', '512GB'),
    ('1TB', '1TB'),
]

RAM_CHOICES = [
    ('4GB', '4GB'),
    ('6GB', '6GB'),
    ('8GB', '8GB'),
    ('12GB', '12GB'),
    ('16GB', '16GB'),
    ('32GB', '32GB'),
]

BRAND_CHOICES = [
    ('Google', 'Google'),
    ('Samsung', 'Samsung'),
]

ORDER_STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('shipped', 'Shipping'),
    ('delivered', 'Delivered'),
    ('cancelled', 'Cancelled'),
]

PAYMENT_STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('paid', 'Paid'),
    ('failed', 'Failed'),
]

CONTACT_STATUS_CHOICES = [
    ('new', 'New'),
    ('read', 'Read'),
    ('replied', 'Replied'),
    ('archived', 'Archived'),
]


class Phone(models.Model):
    """Phone model representing mobile devices with optimized structure."""
    
    # Basic Information
    brand = models.CharField(
        max_length=10, 
        choices=BRAND_CHOICES, 
        default="Samsung",
        verbose_name="Brand",
        db_index=True  # Added index for filtering
    )
    model = models.CharField(
        max_length=20, 
        verbose_name="Model",
        db_index=True  # Added index for searching
    )

    # Network and Connectivity
    physical_sim = models.BooleanField(default=False, verbose_name="Physical SIM Support")
    esim = models.BooleanField(default=False, verbose_name="eSIM Support")
    number_of_sims = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])

    support_5G = models.BooleanField(default=False, verbose_name="5G Support")
    Fingerprint = models.BooleanField(default=False, verbose_name="Fingerprint Support")
    FaceScanner = models.BooleanField(default=False, verbose_name="Facescanner Support")
    Iris_scanner = models.BooleanField(default=False, verbose_name="IrisScanner Support")

    # Display
    display_size = models.CharField(max_length=50, default="6.1")

    # Camera
    camera_details = models.TextField(default="12MP wide, 12MP ultra-wide")

    # Battery
    battery_capacity = models.CharField(max_length=50, default="4000")

    # Images
    thumbnail = models.ImageField(upload_to='media/product_images/', blank=True, null=True)
    photo_1 = models.ImageField(upload_to='media/product_images/', blank=True, null=True)
    photo_2 = models.ImageField(upload_to='media/product_images', blank=True, null=True)
    photo_3 = models.ImageField(upload_to='media/product_images/', blank=True, null=True)

    link = models.URLField(blank=True, null=True, verbose_name="Product Link")
    
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'store_phone'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['brand', 'model']),
            models.Index(fields=['created_at']),
            models.Index(fields=['brand']),
        ]
        verbose_name = 'Phone'
        verbose_name_plural = 'Phones'

    def __str__(self):
        return f"{self.brand} {self.model}"

    def get_min_price(self):
        """Get minimum price among all storage variants."""
        if hasattr(self, '_min_price'):
            return self._min_price
        min_price = self.storage_variants.aggregate(min_price=Min('price'))['min_price']
        return min_price or 0

    def get_max_price(self):
        """Get maximum price among all storage variants."""
        if hasattr(self, '_max_price'):
            return self._max_price
        max_price = self.storage_variants.aggregate(max_price=Max('price'))['max_price']
        return max_price or 0

    def get_storage_options(self):
        """Ultra-fast storage options using values_list (if you only need basic info)."""
        # Single query, returns tuples - fastest possible
        variants = self.storage_variants.values_list(
            'storage_capacity', 
            'price', 
            'old_price',
            'id'
        ).distinct()
        
        storage_options = []
        for capacity, price, old_price, variant_id in variants:
            storage_options.append({
                'storage_capacity': capacity,
                'price': float(price),
                'old_price': float(old_price) if old_price else None,
                'id': variant_id
            })
        
        return storage_options

    def get_available_rams(self):
        """Get list of available RAM options."""
        return list(self.ram_variants.values_list('ram', flat=True).distinct())

    def has_discount_variants(self):
        """Check if any storage variant has a discount."""
        return self.storage_variants.filter(old_price__isnull=False, old_price__gt=0).exists()

    def get_max_discount(self):
        """Get maximum discount percentage among variants."""
        from django.db.models import F, ExpressionWrapper, FloatField
        variants_with_discount = self.storage_variants.filter(
            old_price__isnull=False, 
            old_price__gt=0
        ).annotate(
            discount_percentage=ExpressionWrapper(
                (1 - F('price') / F('old_price')) * 100,
                output_field=FloatField()
            )
        ).order_by('-discount_percentage').first()
        
        return int(variants_with_discount.discount_percentage) if variants_with_discount else 0

    @property
    def is_available(self):
        """Check if phone has any variants in stock."""
        return self.storage_variants.filter(stock__gt=0).exists()


class RAMVariant(models.Model):
    """RAM variant model for phone memory configurations."""
    
    phone = models.ForeignKey(
        Phone, 
        on_delete=models.CASCADE, 
        related_name='ram_variants',
        db_index=True
    )
    ram = models.CharField(
        max_length=10, 
        choices=RAM_CHOICES, 
        verbose_name="RAM",
        db_index=True
    )

    class Meta:
        db_table = 'store_ram_variant'
        unique_together = ('phone', 'ram')
        indexes = [
            models.Index(fields=['phone', 'ram']),
        ]
        verbose_name = 'RAM Variant'
        verbose_name_plural = 'RAM Variants'

    def __str__(self):
        return f"{self.phone.brand} {self.phone.model} - {self.ram}"


class StorageVariant(models.Model):
    """Storage variant model for phone storage configurations with pricing."""
    
    phone = models.ForeignKey(
        Phone, 
        on_delete=models.CASCADE, 
        related_name='storage_variants',
        db_index=True
    )
    ram = models.ForeignKey(
        RAMVariant, 
        on_delete=models.CASCADE, 
        related_name='storage_variants',
        db_index=True
    )
    storage_capacity = models.CharField(
        max_length=10, 
        choices=STORAGE_CHOICES, 
        verbose_name="Storage Capacity",
        db_index=True
    )
    stock = models.PositiveIntegerField(
        default=0,
        validators=[MinValueValidator(0)]
    )
    price = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        validators=[MinValueValidator(0.01)],
        verbose_name="Price"
    )
    old_price = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        validators=[MinValueValidator(0.01)],
        null=True,
        blank=True,
        verbose_name="Old Price"
    )

    class Meta:
        db_table = 'store_storage_variant'
        unique_together = ('ram', 'storage_capacity')
        indexes = [
            models.Index(fields=['phone', 'ram']),
            models.Index(fields=['storage_capacity']),
            models.Index(fields=['price']),
            models.Index(fields=['stock']),
        ]
        verbose_name = 'Storage Variant'
        verbose_name_plural = 'Storage Variants'

    def __str__(self):
        return f"{self.phone.brand} {self.phone.model} - {self.storage_capacity} - GHS {self.price}"

    def clean(self):
        """Validate that storage variant belongs to the same phone as RAM variant."""
        if self.ram and self.phone and self.ram.phone != self.phone:
            raise ValidationError("RAM variant must belong to the same phone")

    def save(self, *args, **kwargs):
        """Validate before saving."""
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def has_discount(self):
        """Check if variant has discount."""
        return self.old_price is not None and self.old_price > self.price

    @property
    def discount_percentage(self):
        """Calculate discount percentage."""
        if self.has_discount:
            return int((1 - (self.price / self.old_price)) * 100)
        return 0

    @property
    def is_in_stock(self):
        """Check if variant is in stock."""
        return self.stock > 0

    def reduce_stock(self, quantity=1):
        """Reduce stock by specified quantity."""
        if quantity > self.stock:
            raise ValidationError(f"Insufficient stock. Available: {self.stock}")
        
        self.stock -= quantity
        self.save(update_fields=['stock'])

    def increase_stock(self, quantity=1):
        """Increase stock by specified quantity."""
        self.stock += quantity
        self.save(update_fields=['stock'])


class Order(models.Model):
    """Order model for customer purchases."""
    PAYMENT_METHODS = [
        ('mobile_money', 'Mobile Money'),
        ('btc', 'Bitcoin'),
        ('cash_on_delivery', 'Cash on Delivery'),
    ]

    order_number = models.CharField(max_length=50, unique=True, editable=False, db_index=True)
    
    # Customer Information
    customer_name = models.CharField(max_length=255)
    customer_email = models.EmailField(db_index=True)
    customer_phone_number = models.CharField(max_length=15)
    
    # Delivery Information
    street_address = models.CharField(max_length=255)
    town = models.CharField(max_length=100)
    region = models.CharField(max_length=100)
    postal_code = models.CharField(max_length=20, blank=True)
    delivery_address = models.TextField(editable=False)
    
    # Order Details
    estimated_delivery = models.DateTimeField(null=True, blank=True)
    payment_method = models.CharField(
        max_length=20, 
        choices=PAYMENT_METHODS,
        default='mobile_money',
        db_index=True
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Status Fields
    status = models.CharField(
        max_length=20,
        choices=ORDER_STATUS_CHOICES,
        default='pending',
        db_index=True
    )
    payment_reference = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    payment_status = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS_CHOICES,
        default='pending',
        db_index=True
    )

    class Meta:
        db_table = 'store_order'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer_email']),
            models.Index(fields=['status', 'payment_status']),
            models.Index(fields=['created_at', 'status']),
        ]
        verbose_name = 'Order'
        verbose_name_plural = 'Orders'

    def __str__(self):
        return f"Order #{self.order_number} ({self.customer_name})"

    def clean(self):
        """Validate order data."""
        if self.payment_method == 'cash_on_delivery' and self.region != 'Greater Accra':
            raise ValidationError('Cash on Delivery is only available for Greater Accra.')

    def save(self, *args, **kwargs):
        """Generate order number and delivery address before saving."""
        self.full_clean()
        
        if not self.order_number:
            self.order_number = self._generate_order_number()
        
        if not self.delivery_address:
            self.delivery_address = self._generate_delivery_address()
            
        if not self.estimated_delivery:
            self.estimated_delivery = timezone.now() + timezone.timedelta(days=5)
            
        super().save(*args, **kwargs)

    def _generate_order_number(self):
        """Generate unique order number."""
        return f"iTRADEORD-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

    def _generate_delivery_address(self):
        """Generate formatted delivery address."""
        parts = [self.street_address, self.town, self.region]
        if self.postal_code:
            parts.append(self.postal_code)
        return ', '.join(filter(None, parts))

    @property
    def total_quantity(self):
        """Calculate total quantity of items in order."""
        if hasattr(self, '_total_quantity'):
            return self._total_quantity
        return sum(item.quantity for item in self.items.all())

    @property
    def total_price(self):
        """Calculate total price of order."""
        if hasattr(self, '_total_price'):
            return self._total_price
        return sum(item.total_price for item in self.items.all())

    def get_status_display(self):
        """Get human-readable status."""
        return dict(ORDER_STATUS_CHOICES).get(self.status, self.status)

    def get_payment_method_display(self):
        """Get human-readable payment method."""
        return dict(self.PAYMENT_METHODS).get(self.payment_method, self.payment_method)

    @transaction.atomic
    def add_item(self, phone, memory_variant, quantity=1):
        """Add item to order with stock validation."""
        if quantity > memory_variant.stock:
            raise ValidationError(f"Insufficient stock. Available: {memory_variant.stock}")
        
        OrderItem.objects.create(
            order=self,
            phone=phone,
            memory_variant=memory_variant,
            quantity=quantity,
            price_at_purchase=memory_variant.price
        )

    @transaction.atomic
    def mark_as_paid(self, payment_reference=None):
        """Mark order as paid and reduce stock."""
        self.payment_status = 'paid'
        if payment_reference:
            self.payment_reference = payment_reference
        
        # Reduce stock for all items
        for item in self.items.select_related('memory_variant').all():
            item.memory_variant.reduce_stock(item.quantity)
        
        self.save(update_fields=['payment_status', 'payment_reference'])


class OrderItem(models.Model):
    """Order item model representing individual products in an order."""
    
    order = models.ForeignKey(
        Order, 
        on_delete=models.CASCADE, 
        related_name='items',
        db_index=True
    )
    phone = models.ForeignKey(
        Phone, 
        on_delete=models.PROTECT,
        db_index=True
    )
    memory_variant = models.ForeignKey(
        StorageVariant, 
        on_delete=models.PROTECT,
        db_index=True
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)]
    )
    price_at_purchase = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        validators=[MinValueValidator(0.01)]
    )

    class Meta:
        db_table = 'store_orderitem'
        unique_together = ('order', 'phone', 'memory_variant')
        indexes = [
            models.Index(fields=['order', 'phone']),
        ]
        verbose_name = 'Order Item'
        verbose_name_plural = 'Order Items'

    def __str__(self):
        return f"{self.quantity}x {self.phone.brand} {self.phone.model} ({self.memory_variant.storage_capacity})"

    def clean(self):
        """Validate order item."""
        if self.memory_variant.phone != self.phone:
            raise ValidationError("Memory variant must belong to the selected phone")

    def save(self, *args, **kwargs):
        """Validate before saving."""
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def total_price(self):
        """Calculate total price for this line item."""
        return self.price_at_purchase * self.quantity

    @property
    def product_name(self):
        """Get formatted product name."""
        return f"{self.phone.brand} {self.phone.model} {self.memory_variant.storage_capacity}"


class ContactMessage(models.Model):
    """Contact message model for customer inquiries."""
    
    name = models.CharField(max_length=100)
    email = models.EmailField(db_index=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    subject = models.CharField(max_length=200)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    status = models.CharField(
        max_length=10, 
        choices=CONTACT_STATUS_CHOICES, 
        default='new',
        db_index=True
    )

    class Meta:
        db_table = 'store_contactmessage'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['status', 'created_at']),
        ]
        verbose_name = 'Contact Message'
        verbose_name_plural = 'Contact Messages'

    def __str__(self):
        return f"Message from {self.name} - {self.subject}"

    def get_status_display(self):
        """Get human-readable status."""
        return dict(CONTACT_STATUS_CHOICES).get(self.status, self.status)

    @property
    def is_unread(self):
        """Check if message is unread."""
        return self.status == 'new'


class Visit(models.Model):
    """Visit model for tracking website analytics."""
    
    ip_address = models.CharField(max_length=32, db_index=True)
    user_agent = models.CharField(max_length=255)
    referrer = models.URLField(blank=True, null=True)
    session_id = models.CharField(max_length=40, blank=True, null=True, db_index=True)
    user = models.ForeignKey(
        'auth.User', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        db_index=True
    )
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    path = models.CharField(max_length=255, db_index=True)
    is_bot = models.BooleanField(default=False)

    class Meta:
        db_table = 'store_visit'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['timestamp']),
            models.Index(fields=['ip_address']),
            models.Index(fields=['user']),
            models.Index(fields=['path', 'timestamp']),
            models.Index(fields=['is_bot', 'timestamp']),
        ]
        verbose_name = 'Visit'
        verbose_name_plural = 'Visits'

    def save(self, *args, **kwargs):
        """Hash IP address and check for bots before saving."""
        if self.ip_address:
            self.ip_address = hashlib.sha256(self.ip_address.encode()).hexdigest()[:32]
        
        if not self.is_bot and self.user_agent:
            self.is_bot = self.is_bot_user_agent(self.user_agent)
            
        super().save(*args, **kwargs)

    @classmethod
    def is_bot_user_agent(cls, user_agent):
        """Check if user agent belongs to a bot."""
        if not user_agent:
            return False
            
        bots = ['bot', 'crawl', 'spider', 'slurp', 'googlebot', 'bingbot', 'yandexbot']
        user_agent_lower = user_agent.lower()
        return any(bot in user_agent_lower for bot in bots)

    @property
    def is_authenticated_visit(self):
        """Check if visit is from authenticated user."""
        return self.user is not None