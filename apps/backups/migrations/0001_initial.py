import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('tenants', '0006_business_onboarding_banner_dismissed_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='BackupRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('public_id', models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                ('tenant_public_id_snapshot', models.UUIDField(db_index=True, editable=False)),
                ('scope', models.CharField(choices=[('POS', 'POS'), ('WMS', 'WMS'), ('ALL_ENABLED', 'All enabled products')], max_length=20)),
                ('included_products', models.JSONField(blank=True, default=list)),
                ('included_components', models.JSONField(blank=True, default=list)),
                ('trigger', models.CharField(choices=[('MANUAL', 'Manual'), ('SCHEDULED', 'Scheduled'), ('PRE_RESTORE_SAFETY', 'Pre-restore safety')], db_index=True, default='MANUAL', max_length=24)),
                ('scheduled_local_date', models.DateField(blank=True, null=True)),
                ('status', models.CharField(choices=[('QUEUED', 'Queued'), ('PREPARING', 'Preparing'), ('SNAPSHOTTING', 'Snapshotting'), ('PACKAGING', 'Packaging'), ('UPLOADING', 'Uploading'), ('VERIFYING', 'Verifying'), ('SUCCEEDED', 'Succeeded'), ('FAILED', 'Failed'), ('CANCELLED', 'Cancelled'), ('DELETION_PENDING', 'Deletion pending'), ('DELETED', 'Deleted')], db_index=True, default='QUEUED', max_length=24)),
                ('integrity_status', models.CharField(choices=[('NOT_CHECKED', 'Not checked'), ('VERIFYING', 'Verifying'), ('VERIFIED', 'Verified'), ('FAILED', 'Failed'), ('CORRUPTED', 'Corrupted')], db_index=True, default='NOT_CHECKED', max_length=20)),
                ('pinned', models.BooleanField(db_index=True, default=False)),
                ('retention_eligible', models.BooleanField(db_index=True, default=False)),
                ('protected', models.BooleanField(db_index=True, default=False)),
                ('format_version', models.CharField(max_length=32)),
                ('application_version', models.CharField(max_length=64)),
                ('schema_fingerprint', models.CharField(max_length=64)),
                ('minimum_restore_version', models.CharField(max_length=64)),
                ('compatibility_status', models.CharField(choices=[('NOT_CHECKED', 'Not checked'), ('COMPATIBLE', 'Compatible'), ('REQUIRES_UPGRADE', 'Requires upgrade'), ('INCOMPATIBLE', 'Incompatible')], default='NOT_CHECKED', max_length=24)),
                ('restore_compatibility_reason', models.CharField(blank=True, max_length=500)),
                ('storage_backend_identifier', models.CharField(blank=True, max_length=80)),
                ('opaque_object_key', models.CharField(blank=True, max_length=500)),
                ('encryption_key_identifier', models.CharField(blank=True, max_length=255)),
                ('encrypted_data_key_envelope', models.TextField(blank=True)),
                ('whole_artifact_hash', models.CharField(blank=True, max_length=128)),
                ('total_row_count', models.PositiveBigIntegerField(default=0)),
                ('component_count', models.PositiveIntegerField(default=0)),
                ('media_count', models.PositiveBigIntegerField(default=0)),
                ('backup_size_bytes', models.PositiveBigIntegerField(default=0)),
                ('duration', models.DurationField(blank=True, null=True)),
                ('queued_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('verified_at', models.DateTimeField(blank=True, null=True)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('creator_actor_snapshot', models.JSONField(blank=True, default=dict)),
                ('system_actor', models.BooleanField(default=False)),
                ('failure_code', models.CharField(blank=True, max_length=80)),
                ('sanitized_failure_summary', models.TextField(blank=True)),
                ('idempotency_key', models.CharField(max_length=128)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='backup_records', to='tenants.business')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='backup_records_created', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
                'permissions': [('platform_view_metadata', 'Can view platform backup metadata'), ('platform_manage_backups', 'Can manage platform backups'), ('platform_approve_restore', 'Can approve platform restores'), ('platform_cleanup_backups', 'Can clean up platform backup artifacts')],
            },
        ),
        migrations.CreateModel(
            name='BackupSchedule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('enabled', models.BooleanField(db_index=True, default=False)),
                ('timezone_name', models.CharField(max_length=64)),
                ('local_execution_time', models.TimeField()),
                ('next_run', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('last_claimed_run', models.DateTimeField(blank=True, null=True)),
                ('scope', models.CharField(choices=[('POS', 'POS'), ('WMS', 'WMS'), ('ALL_ENABLED', 'All enabled products')], default='ALL_ENABLED', max_length=20)),
                ('business', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='backup_schedule', to='tenants.business')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='backup_schedules_created', to=settings.AUTH_USER_MODEL)),
                ('last_failed_backup', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='schedules_as_last_failure', to='backups.backuprecord')),
                ('last_successful_backup', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='schedules_as_last_success', to='backups.backuprecord')),
            ],
            options={
                'ordering': ['business_id'],
            },
        ),
        migrations.CreateModel(
            name='DownloadGrant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                ('token_hash', models.CharField(max_length=128, unique=True)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('used_at', models.DateTimeField(blank=True, null=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('single_use', models.BooleanField(default=True)),
                ('request_ip', models.GenericIPAddressField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('backup', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='download_grants', to='backups.backuprecord')),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='backup_download_grants', to='tenants.business')),
                ('issued_to', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='backup_download_grants', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='RestoreOperation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('public_id', models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                ('requested_scope', models.CharField(choices=[('POS', 'POS'), ('WMS', 'WMS'), ('ALL_ENABLED', 'All enabled products')], max_length=20)),
                ('status', models.CharField(choices=[('QUEUED', 'Queued'), ('AUTHORIZING', 'Authorizing'), ('LOCKING', 'Locking'), ('SAFETY_BACKUP', 'Creating safety backup'), ('VALIDATING', 'Validating'), ('RESTORING', 'Restoring'), ('VERIFYING', 'Verifying'), ('SUCCEEDED', 'Succeeded'), ('FAILED', 'Failed'), ('ROLLING_BACK', 'Rolling back'), ('ROLLED_BACK', 'Rolled back'), ('INDETERMINATE', 'Indeterminate')], db_index=True, default='QUEUED', max_length=24)),
                ('actor_identity_snapshot', models.JSONField(blank=True, default=dict)),
                ('reason', models.CharField(max_length=500)),
                ('dependency_check_status', models.CharField(choices=[('NOT_CHECKED', 'Not checked'), ('CHECKING', 'Checking'), ('PASSED', 'Passed'), ('FAILED', 'Failed')], default='NOT_CHECKED', max_length=20)),
                ('compatibility_status', models.CharField(choices=[('NOT_CHECKED', 'Not checked'), ('COMPATIBLE', 'Compatible'), ('REQUIRES_UPGRADE', 'Requires upgrade'), ('INCOMPATIBLE', 'Incompatible')], default='NOT_CHECKED', max_length=24)),
                ('compatibility_reason', models.CharField(blank=True, max_length=500)),
                ('rollback_attempted', models.BooleanField(default=False)),
                ('rollback_result', models.CharField(blank=True, max_length=500)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('failure_code', models.CharField(blank=True, max_length=80)),
                ('sanitized_failure_summary', models.TextField(blank=True)),
                ('idempotency_key', models.CharField(max_length=128)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='restore_operations', to='tenants.business')),
                ('requested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='restore_operations_requested', to=settings.AUTH_USER_MODEL)),
                ('safety_backup', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='restore_operations_as_safety', to='backups.backuprecord')),
                ('source_backup', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='restore_operations', to='backups.backuprecord')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddField(
            model_name='backuprecord',
            name='parent_restore_operation',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='safety_backup_records', to='backups.restoreoperation'),
        ),
        migrations.CreateModel(
            name='BackupActivity',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                ('event_type', models.CharField(db_index=True, max_length=100)),
                ('severity', models.CharField(choices=[('INFO', 'Info'), ('WARNING', 'Warning'), ('ERROR', 'Error'), ('CRITICAL', 'Critical')], db_index=True, default='INFO', max_length=12)),
                ('actor_identity_snapshot', models.JSONField(blank=True, default=dict)),
                ('support_actor_identity_snapshot', models.JSONField(blank=True, default=dict)),
                ('reason', models.CharField(blank=True, max_length=500)),
                ('sanitized_message', models.CharField(blank=True, max_length=500)),
                ('structured_metadata', models.JSONField(blank=True, default=dict)),
                ('request_ip', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.CharField(blank=True, max_length=300)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='backup_activities', to=settings.AUTH_USER_MODEL)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='backup_activities', to='tenants.business')),
                ('backup', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='activities', to='backups.backuprecord')),
                ('restore', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='activities', to='backups.restoreoperation')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='TenantOperationLock',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('operation_kind', models.CharField(choices=[('BACKUP', 'Backup'), ('RESTORE', 'Restore'), ('RETENTION', 'Retention'), ('DOWNLOAD', 'Download'), ('VERIFICATION', 'Verification')], max_length=20)),
                ('operation_public_id', models.UUIDField(db_index=True)),
                ('lock_token', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('worker_task_identifier', models.CharField(blank=True, max_length=255)),
                ('acquired_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('lease_expires_at', models.DateTimeField(db_index=True)),
                ('heartbeat_at', models.DateTimeField(blank=True, null=True)),
                ('released_at', models.DateTimeField(blank=True, null=True)),
                ('active', models.BooleanField(db_index=True, default=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='operation_locks', to='tenants.business')),
            ],
            options={
                'ordering': ['-acquired_at'],
            },
        ),
        migrations.CreateModel(
            name='BackupComponent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('component_key', models.CharField(max_length=120)),
                ('product_category', models.CharField(choices=[('SHARED', 'Shared'), ('POS', 'POS'), ('WMS', 'WMS')], max_length=12)),
                ('component_version', models.CharField(max_length=32)),
                ('row_count', models.PositiveBigIntegerField(default=0)),
                ('media_count', models.PositiveBigIntegerField(default=0)),
                ('uncompressed_size', models.PositiveBigIntegerField(default=0)),
                ('compressed_size', models.PositiveBigIntegerField(default=0)),
                ('component_hash', models.CharField(blank=True, max_length=128)),
                ('verification_status', models.CharField(choices=[('NOT_CHECKED', 'Not checked'), ('VERIFYING', 'Verifying'), ('VERIFIED', 'Verified'), ('FAILED', 'Failed'), ('CORRUPTED', 'Corrupted')], default='NOT_CHECKED', max_length=20)),
                ('verification_summary', models.CharField(blank=True, max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('backup', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='components', to='backups.backuprecord')),
            ],
            options={
                'ordering': ['backup_id', 'component_key'],
                'constraints': [models.UniqueConstraint(fields=('backup', 'component_key'), name='uniq_backup_component')],
            },
        ),
        migrations.AddIndex(
            model_name='restoreoperation',
            index=models.Index(fields=['business', '-created_at'], name='restore_business_created_idx'),
        ),
        migrations.AddIndex(
            model_name='restoreoperation',
            index=models.Index(fields=['business', 'status'], name='restore_business_state_idx'),
        ),
        migrations.AddConstraint(
            model_name='restoreoperation',
            constraint=models.UniqueConstraint(fields=('business', 'idempotency_key'), name='uniq_restore_idempotency'),
        ),
        migrations.AddConstraint(
            model_name='restoreoperation',
            constraint=models.CheckConstraint(condition=models.Q(('safety_backup__isnull', True), models.Q(('safety_backup', models.F('source_backup')), _negated=True), _connector='OR'), name='restore_safety_not_source'),
        ),
        migrations.AddIndex(
            model_name='backuprecord',
            index=models.Index(fields=['business', '-created_at'], name='backup_business_created_idx'),
        ),
        migrations.AddIndex(
            model_name='backuprecord',
            index=models.Index(fields=['business', 'status', 'integrity_status'], name='backup_business_state_idx'),
        ),
        migrations.AddConstraint(
            model_name='backuprecord',
            constraint=models.UniqueConstraint(fields=('business', 'idempotency_key'), name='uniq_backup_idempotency'),
        ),
        migrations.AddConstraint(
            model_name='backuprecord',
            constraint=models.UniqueConstraint(condition=models.Q(('opaque_object_key', ''), _negated=True), fields=('opaque_object_key',), name='uniq_backup_object_key'),
        ),
        migrations.AddConstraint(
            model_name='backuprecord',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('trigger', 'SCHEDULED'), _negated=True), ('scheduled_local_date__isnull', False), _connector='OR'), name='scheduled_backup_has_date'),
        ),
        migrations.AddConstraint(
            model_name='backuprecord',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('trigger', 'PRE_RESTORE_SAFETY'), _negated=True), models.Q(('protected', True), ('retention_eligible', False), ('parent_restore_operation__isnull', False)), _connector='OR'), name='safety_backup_is_protected'),
        ),
        migrations.AddConstraint(
            model_name='backuprecord',
            constraint=models.CheckConstraint(condition=models.Q(('retention_eligible', False), models.Q(('trigger', 'SCHEDULED'), ('scope', 'ALL_ENABLED'), ('status', 'SUCCEEDED'), ('integrity_status', 'VERIFIED'), ('pinned', False), ('protected', False)), _connector='OR'), name='valid_backup_retention'),
        ),
        migrations.AddConstraint(
            model_name='backuprecord',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('status', 'SUCCEEDED'), _negated=True), ('integrity_status__in', ('VERIFIED', 'CORRUPTED')), _connector='OR'), name='successful_backup_verified'),
        ),
        migrations.AddConstraint(
            model_name='backuprecord',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('status', 'DELETED'), _negated=True), ('deleted_at__isnull', False), _connector='OR'), name='deleted_backup_has_time'),
        ),
        migrations.AddIndex(
            model_name='backupactivity',
            index=models.Index(fields=['business', '-created_at'], name='activity_business_created_idx'),
        ),
        migrations.AddConstraint(
            model_name='tenantoperationlock',
            constraint=models.UniqueConstraint(condition=models.Q(('active', True)), fields=('business',), name='uniq_active_tenant_op_lock'),
        ),
        migrations.AddConstraint(
            model_name='tenantoperationlock',
            constraint=models.CheckConstraint(condition=models.Q(('lease_expires_at__gt', models.F('acquired_at'))), name='lock_lease_after_acquired'),
        ),
        migrations.AddConstraint(
            model_name='tenantoperationlock',
            constraint=models.CheckConstraint(condition=models.Q(('active', False), ('released_at__isnull', True), _connector='OR'), name='active_lock_not_released'),
        ),
    ]
