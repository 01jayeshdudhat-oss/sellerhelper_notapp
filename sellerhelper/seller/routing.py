from django.urls import path

from .consumers import LiveUpdatesConsumer


websocket_urlpatterns = [
    path("ws/live/", LiveUpdatesConsumer.as_asgi(), name="live_updates"),
]
