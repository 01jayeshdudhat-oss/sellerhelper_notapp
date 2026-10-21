from django.db import migrations, models


def split_navigation_setting(apps, schema_editor):
    SiteThemeSettings = apps.get_model("seller", "SiteThemeSettings")
    SiteThemeRevision = apps.get_model("seller", "SiteThemeRevision")
    database = schema_editor.connection.alias

    for theme in SiteThemeSettings.objects.using(database).all():
        theme.mobile_topbar_scroll_hide_enabled = (
            theme.mobile_navigation_scroll_hide_enabled
        )
        theme.mobile_bottombar_scroll_hide_enabled = (
            theme.mobile_navigation_scroll_hide_enabled
        )
        theme.save(
            using=database,
            update_fields=[
                "mobile_topbar_scroll_hide_enabled",
                "mobile_bottombar_scroll_hide_enabled",
            ],
        )

    for revision in SiteThemeRevision.objects.using(database).all():
        snapshot = revision.snapshot
        if not isinstance(snapshot, dict):
            continue
        enabled = bool(snapshot.pop("mobile_navigation_scroll_hide_enabled", False))
        snapshot.setdefault("mobile_topbar_scroll_hide_enabled", enabled)
        snapshot.setdefault("mobile_bottombar_scroll_hide_enabled", enabled)
        revision.snapshot = snapshot
        revision.save(using=database, update_fields=["snapshot"])


def combine_navigation_settings(apps, schema_editor):
    SiteThemeSettings = apps.get_model("seller", "SiteThemeSettings")
    SiteThemeRevision = apps.get_model("seller", "SiteThemeRevision")
    database = schema_editor.connection.alias

    for theme in SiteThemeSettings.objects.using(database).all():
        theme.mobile_navigation_scroll_hide_enabled = (
            theme.mobile_topbar_scroll_hide_enabled
            or theme.mobile_bottombar_scroll_hide_enabled
        )
        theme.save(
            using=database,
            update_fields=["mobile_navigation_scroll_hide_enabled"],
        )

    for revision in SiteThemeRevision.objects.using(database).all():
        snapshot = revision.snapshot
        if not isinstance(snapshot, dict):
            continue
        enabled = bool(
            snapshot.get("mobile_topbar_scroll_hide_enabled", False)
            or snapshot.get("mobile_bottombar_scroll_hide_enabled", False)
        )
        snapshot.pop("mobile_topbar_scroll_hide_enabled", None)
        snapshot.pop("mobile_bottombar_scroll_hide_enabled", None)
        snapshot["mobile_navigation_scroll_hide_enabled"] = enabled
        revision.snapshot = snapshot
        revision.save(using=database, update_fields=["snapshot"])


class Migration(migrations.Migration):

    dependencies = [
        ("seller", "0096_sitethemesettings_mobile_navigation_scroll_hide_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitethemesettings",
            name="mobile_topbar_scroll_hide_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="sitethemesettings",
            name="mobile_bottombar_scroll_hide_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(
            split_navigation_setting,
            reverse_code=combine_navigation_settings,
        ),
        migrations.RemoveField(
            model_name="sitethemesettings",
            name="mobile_navigation_scroll_hide_enabled",
        ),
    ]
