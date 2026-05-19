# celery.py (in your Django project)
from __future__ import absolute_import
import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'your_project.settings')

app = Celery('your_project')
app.config_from_object('django.conf:settings', namespace='CELERY')

# Broker settings (RabbitMQ)
app.conf.broker_url = 'amqp://localhost:5672//'
app.conf.result_backend = 'rpc://'
app.conf.task_default_queue = 'default'

app.autodiscover_tasks()