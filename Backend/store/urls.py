# store/urls.py
from django.urls import path
from .views import (
    # Existing views
    ContactMessageList, ContactMessageCreate, ContactMessageDetail, ContactMessageStatusUpdate, OrderList,
    PhoneList, PhoneDetail, CreateOrder, OrderDetail, AnalyticsVisitsView, StorageSelector, StorageVariantDetail, StorageVariantList, RAMVariantList, FeaturedList, VerifyPaymentView,
    # New authentication views
    LoginView, LogoutView, CurrentUserView
)

urlpatterns = [
    # Authentication Endpoints
    path('auth/login/', LoginView.as_view(), name='login'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    path('auth/me/', CurrentUserView.as_view(), name='current-user'),
    
    # Phone Endpoints
    path('phones/', PhoneList.as_view(), name='phone-list'),
    path('featured/', FeaturedList.as_view(), name='featured-list'),
    path('phones/<int:pk>/', PhoneDetail.as_view(), name='phone-detail'),
    path('phones/brand/<str:brand>/', PhoneList.as_view(), name='brand'),
    
    # Variant Endpoints
    path('phone/storage/', StorageVariantList.as_view(), name='storage-list'),
    path('phone/ram/', RAMVariantList.as_view(), name='ram-list'),
    path('phone/storage-selector/<int:phone_id>/<int:ram_id>/', StorageSelector.as_view(), name='storage-selector'),
    path('phone/storage-selector/<int:phone_id>/', StorageSelector.as_view(), name='storage-selector'),
    # urls.py
    path('phone/storage/<int:pk>/', StorageVariantDetail.as_view(), name='storage-variant-detail'),
    # Order Endpoints
    path('order/', CreateOrder.as_view(), name='create-order'),
    path('orders/', OrderList.as_view(), name='list-orders'),
    path('orders/<int:pk>/', OrderDetail.as_view(), name='order-detail'),
    path('verify-payment/', VerifyPaymentView.as_view(), name='verify-payment'),
    # Contact Endpoints
    path('contact-messages/', ContactMessageList.as_view(), name='contact-messages-list'),
    path('contact-messages/create/', ContactMessageCreate.as_view(), name='contact-message-create'),
    path('contact-messages/<int:pk>/', ContactMessageDetail.as_view(), name='contact-message-detail'),
    path('contact-messages/<int:pk>/status/', ContactMessageStatusUpdate.as_view(), name='contact-message-status-update'),

    #Analytics Endpoints
    path('analytics/visits/', AnalyticsVisitsView.as_view(), name='analytics-visits')
]