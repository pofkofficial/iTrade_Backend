# store/admin.py
from django.contrib import admin
from .models import ContactMessage, Phone, Order, StorageVariant, RAMVariant, OrderItem

class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 1
    readonly_fields = ('price_at_purchase',)
    fields = ('phone', 'memory_variant', 'quantity', 'price_at_purchase')

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    inlines = [OrderItemInline]
    list_display = (
        'order_number', 
        'customer_name', 
        'customer_email',
        'status',  # Replaced is_paid and is_delivered
        'created_at',
        'total_price' 
    )

    list_filter = (
        'status',  # Filter by status instead of is_paid/is_delivered
        'created_at',
        'payment_method'
    )

    search_fields = (
        'order_number', 
        'customer_name', 
        'customer_email',
        'customer_phone_number'
    )

    readonly_fields = (
        'order_number', 
        'delivery_address', 
        'created_at', 
        'updated_at',
        'total_price'
    )

    list_per_page = 20

    #Add custom actions for status changes
    actions = ['mark_as_delivered', 'mark_as_cancelled']

    def mark_as_delivered(self, request, queryset):
        updated = queryset.update(status='delivered')
        self.message_user(request, f"{updated} orders marked as delivered")
    mark_as_delivered.short_description = "Mark selected orders as delivered"

    def mark_as_cancelled(self, request, queryset):
        updated = queryset.update(status='cancelled')
        self.message_user(request, f"{updated} orders marked as cancelled")
    mark_as_cancelled.short_description = "Mark selected orders as cancelled"

@admin.register(Phone)
class PhoneAdmin(admin.ModelAdmin):
    list_display = ('brand', 'model')
    search_fields = ('brand', 'model')

@admin.register(StorageVariant)
class StorageVariantAdmin(admin.ModelAdmin):
    list_display = ('phone', 'storage_capacity', 'price')
    list_filter = ('phone',)
    search_fields = ('phone__brand', 'phone__model', 'storage_capacity')

@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'subject', 'created_at', 'status')
    list_filter = ('status', 'created_at')
    search_fields = ('name', 'email', 'subject', 'message')
    readonly_fields = ('created_at',)
    list_editable = ('status',)
    actions = ['mark_as_read', 'mark_as_replied']
    
    def mark_as_read(self, request, queryset):
        queryset.update(status='read')
    
    def mark_as_replied(self, request, queryset):
        queryset.update(status='replied')


#admin.site.register(Phone)
#admin.site.register(StorageVariant)
admin.site.register(RAMVariant)
admin.site.register(OrderItem)
#admin.site.register(Order, OrderAdmin)