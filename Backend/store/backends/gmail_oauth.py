import os
import base64
import smtplib
import ssl
import logging
import time
from socket import gaierror
from django.conf import settings
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from django.core.mail.backends.smtp import EmailBackend

logger = logging.getLogger(__name__)

class GmailOAuthBackend(EmailBackend):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.username = kwargs.get('username', settings.EMAIL_HOST_USER)
        self.connection = None
        self.credentials = self._get_credentials()
        self.timeout = 30  # seconds

    def _get_credentials(self):
        """Secure credential handling with automatic refresh"""
        try:
            creds = None
            if os.path.exists(settings.GMAIL_OAUTH_CREDENTIALS_FILE):
                creds = Credentials.from_authorized_user_file(
                    settings.GMAIL_OAUTH_CREDENTIALS_FILE,
                    ['https://mail.google.com/']
                )

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        settings.GMAIL_OAUTH_CLIENT_SECRET_FILE,
                        scopes=['https://mail.google.com/'],
                        redirect_uri='http://localhost:8080/'
                    )
                    creds = flow.run_local_server(port=8080)
                
                with open(settings.GMAIL_OAUTH_CREDENTIALS_FILE, 'w') as token:
                    token.write(creds.to_json())

            return creds
        except Exception as e:
            logger.error(f"Credential error: {str(e)}")
            raise

    def _establish_connection(self):
        """Properly sequenced connection with error recovery"""
        try:
            # First try TLS (port 587)
            smtp = smtplib.SMTP(self.host, 587, timeout=self.timeout)
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            
            # Authenticate
            if self.credentials.expired:
                self.credentials.refresh(Request())

            auth_string = f"user={self.username}\x00auth=Bearer {self.credentials.token}\x00\x00"
            auth_b64 = base64.b64encode(auth_string.encode()).decode()
            
            code, response = smtp.docmd("AUTH", "XOAUTH2 " + auth_b64)
            if code != 235:
                raise smtplib.SMTPAuthenticationError(code, response)

            return smtp

        except smtplib.SMTPResponseException as e:
            if e.smtp_code == 503:  # EHLO/HELO sequence error
                logger.warning("Retrying with corrected protocol sequence...")
                smtp = smtplib.SMTP(self.host, 587, timeout=self.timeout)
                smtp.connect(self.host, 587)  # Connect before EHLO
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                return smtp
            raise

        except Exception:
            # Fallback to SSL (port 465) if TLS fails
            context = ssl.create_default_context()
            smtp = smtplib.SMTP_SSL(
                host=self.host,
                port=465,
                timeout=self.timeout,
                context=context
            )
            return smtp

    def send_messages(self, email_messages):
        """Reliable message sending with proper sequencing"""
        if not email_messages:
            return 0

        try:
            if not self.connection:
                self.connection = self._establish_connection()

            num_sent = 0
            for message in email_messages:
                try:
                    # Verify connection is alive
                    try:
                        self.connection.noop()
                    except:
                        self.connection = self._establish_connection()

                    self.connection.sendmail(
                        message.from_email,
                        message.recipients(),
                        message.message().as_bytes()
                    )
                    num_sent += 1
                except Exception as e:
                    logger.error(f"Error sending message: {str(e)}")
                    if not self.fail_silently:
                        raise

            return num_sent

        except Exception as e:
            logger.error(f"Fatal sending error: {str(e)}")
            if not self.fail_silently:
                raise
            return 0
        finally:
            self.close()

    def close(self):
        """Guaranteed cleanup"""
        if self.connection:
            try:
                self.connection.quit()
            except:
                try:
                    self.connection.close()
                except:
                    pass
            self.connection = None