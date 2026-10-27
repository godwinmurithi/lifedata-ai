import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('lifedata_ai', '0009_boundary_catalogue')]
    operations = [migrations.CreateModel(name='DashboardBuild', fields=[
        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ('username', models.CharField(max_length=255, db_index=True)),
        ('specification', models.JSONField(default=dict)),
        ('status', models.CharField(max_length=20, default='draft', db_index=True)),
        ('review', models.JSONField(default=dict)),
        ('fingerprint', models.CharField(max_length=64, blank=True)),
        ('created_at', models.DateTimeField(auto_now_add=True)),
        ('updated_at', models.DateTimeField(auto_now=True)),
        ('conversation', models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, to='lifedata_ai.conversation')),
        ('publication', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='builds', to='lifedata_ai.dashboardpublication')),
    ], options={'ordering':['-created_at']})]
