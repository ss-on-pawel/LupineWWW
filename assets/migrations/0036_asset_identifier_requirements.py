from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("assets", "0035_historical_withdrawals")]
    operations = [
        migrations.AlterField(model_name="asset", name="inventory_number", field=models.CharField(blank=True, max_length=100, verbose_name="Numer inwentarzowy")),
        migrations.AlterField(model_name="asset", name="barcode", field=models.CharField(max_length=120, verbose_name="Kod kreskowy")),
    ]
