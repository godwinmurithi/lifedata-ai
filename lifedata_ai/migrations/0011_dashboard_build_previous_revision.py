from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies=[('lifedata_ai','0010_dashboard_build')]
    operations=[migrations.AddField(model_name='dashboardbuild',name='previous_revision',field=models.JSONField(default=dict))]
