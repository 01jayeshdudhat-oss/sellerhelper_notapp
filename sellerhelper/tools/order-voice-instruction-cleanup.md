# Order voice instruction cleanup

Voice recordings are stored outside `MEDIA_ROOT` and are served only through the authenticated shop-order audio endpoint. Playback is denied once an order has been delivered for one hour.

Schedule this management command to run every minute so expired files are physically removed within about one minute after the one-hour retention period:

```powershell
D:\sellerhelper\sellerhelper\.venv\Scripts\python.exe D:\sellerhelper\sellerhelper\manage.py cleanup_order_voice_instructions
```

For another deployment environment, run `python manage.py cleanup_order_voice_instructions` from the Django project directory using the application's configured Python environment. The command also removes abandoned, unplaced checkout recordings older than 24 hours.
