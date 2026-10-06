from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('lifedata_ai', '0007_dashboardpublication_configuration')]
    operations = [migrations.AlterField(
        model_name='dashboardpublication', name='superset_dashboard_id',
        field=models.PositiveIntegerField(blank=True, db_index=True, null=True))]
