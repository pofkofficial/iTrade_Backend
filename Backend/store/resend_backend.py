# store/resend_backend.py
import resend
from django.core.mail.backends.base import BaseEmailBackend
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

class ResendBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self.api_key = getattr(settings, 'RESEND_API_KEY', None)
        if self.api_key:
            resend.api_key = self.api_key
        else:
            logger.error("RESEND_API_KEY not configured in settings")

    def send_messages(self, email_messages):
        if not email_messages or not self.api_key:
            logger.warning("No email messages or Resend API key not configured")
            return 0

        sent_count = 0
        for message in email_messages:
            try:
                # Prepare Resend payload
                payload = {
                    'from': message.from_email,
                    'to': message.to,
                    'subject': message.subject,
                    'text': message.body,
                }

                # Add HTML content if available
                if message.alternatives:
                    for content, mimetype in message.alternatives:
                        if mimetype == 'text/html':
                            payload['html'] = content
                            break

                # Send via Resend
                response = resend.Emails.send(payload)
                
                logger.info(f"Resend email sent: {message.subject} to {message.to}")
                logger.debug(f"Resend response: {response}")
                sent_count += 1

            except Exception as e:
                logger.error(f"Resend backend error: {str(e)}")
                if not self.fail_silently:
                    raise

        return sent_count