from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="must_change_password",
            field=models.BooleanField(
                default=False,
                help_text="Set when an administrator creates the account or resets its password.",
                verbose_name="must choose a new password",
            ),
        ),
    ]
