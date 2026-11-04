# store/urls.py
from django.urls import path
from .views import (
    # Existing views
    AdminPhoneList, ContactMessageList, ContactMessageCreate, ContactMessageDetail, ContactMessageStatusUpdate, OrderList,
    PhoneList, PhoneDetail, CreateOrder, OrderDetail, AnalyticsVisitsView, PhoneRestore, StorageSelector, StorageVariantDetail, StorageVariantList, RAMVariantList, FeaturedList, TrackVisitView, VerifyPaymentView,
    # New authentication views
    LoginView, LogoutView, CurrentUserView, PaystackWebhook, AdminCountsView, AdminSummaryView, AdminPaymentReconciliationView, AdminVerifyOrderPaymentView, AdminBulkVariantUpdateView, AdminCustomerListView, AdminDeliverySheetView, AdminClonePhoneView, AdminFunnelView, AdminDemandSignalsView, AdminAnalyticsOverviewView, AdminStoreControlsView, AdminMessageReplyView, AdminSecurityEventsView, AdminAuditEventsView, AdminGlobalSearchView, PublicStoreStatusView
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
    
    # Payment Verification Endpoint
    path('verify-payment/', VerifyPaymentView.as_view(), name='verify-payment'),
      path('paystack/webhook/', PaystackWebhook.as_view(), name='paystack-webhook'),
    # Contact Endpoints
    path('contact-messages/', ContactMessageList.as_view(), name='contact-messages-list'),
    path('contact-messages/create/', ContactMessageCreate.as_view(), name='contact-message-create'),
    path('contact-messages/<int:pk>/', ContactMessageDetail.as_view(), name='contact-message-detail'),
    path('contact-messages/<int:pk>/status/', ContactMessageStatusUpdate.as_view(), name='contact-message-status-update'),


    #Admin Endpoints
    #----Phones
    path('admin/phones/', AdminPhoneList.as_view(), name='admin-phone-list'),
    path('admin/phones/<int:pk>/restore/', PhoneRestore.as_view(), name='phone-restore'),
    path('admin/phones/<int:pk>/clone/', AdminClonePhoneView.as_view(), name='admin-phone-clone'),
    path('admin/counts/', AdminCountsView.as_view(), name='admin-counts'),
    path('admin/summary/', AdminSummaryView.as_view(), name='admin-summary'),
    path('admin/payments/reconciliation/', AdminPaymentReconciliationView.as_view(), name='admin-payment-reconciliation'),
    path('admin/orders/<int:pk>/verify-payment/', AdminVerifyOrderPaymentView.as_view(), name='admin-order-verify-payment'),
    path('admin/variants/bulk/', AdminBulkVariantUpdateView.as_view(), name='admin-variants-bulk'),
    path('admin/customers/', AdminCustomerListView.as_view(), name='admin-customers'),
    path('admin/delivery-sheet/', AdminDeliverySheetView.as_view(), name='admin-delivery-sheet'),
    path('admin/analytics/overview/', AdminAnalyticsOverviewView.as_view(), name='admin-analytics-overview'),
    path('admin/analytics/funnel/', AdminFunnelView.as_view(), name='admin-funnel'),
    path('admin/analytics/demand/', AdminDemandSignalsView.as_view(), name='admin-demand-signals'),
    path('admin/store-controls/', AdminStoreControlsView.as_view(), name='admin-store-controls'),
    path('admin/messages/<int:pk>/reply/', AdminMessageReplyView.as_view(), name='admin-message-reply'),
    path('admin/security-events/', AdminSecurityEventsView.as_view(), name='admin-security-events'),
    path('admin/audit-events/', AdminAuditEventsView.as_view(), name='admin-audit-events'),
    path('admin/search/', AdminGlobalSearchView.as_view(), name='admin-global-search'),
    path('store-status/', PublicStoreStatusView.as_view(), name='public-store-status'),
    #----Analytics Endpoints
    # urls.py
    path('analytics/track/', TrackVisitView.as_view(), name='analytics-track'),
    path('admin/analytics/visits/', AnalyticsVisitsView.as_view(), name='analytics-visits'),
]