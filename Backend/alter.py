from django.core.mail import send_mail
try:
    send_mail(
        'Test Subject', 
        'Test Message',
        'opokulive@gmail.com',
        ['nanakwasifabrice@gmail.com'],
        fail_silently=False
    )
    print("Email sent successfully!")
except Exception as e:
    print(f"Failed to send email: {e}")