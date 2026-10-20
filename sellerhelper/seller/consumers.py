from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings


class LiveUpdatesConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4401)
            return

        self.group_name = f"live_user_{user.pk}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json({
            "type": "connection.ready",
            "shared_channel_layer": settings.CHANNEL_LAYERS["default"]["BACKEND"]
            == "channels_redis.core.RedisChannelLayer",
        })

    async def disconnect(self, close_code):
        group_name = getattr(self, "group_name", None)
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def live_event(self, event):
        await self.send_json({
            "type": event["event_type"],
            **event.get("data", {}),
        })
