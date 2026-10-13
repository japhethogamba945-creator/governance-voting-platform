from django.db import migrations


class Migration(migrations.Migration):
    """Row-level security on app_user (decision log G5.5).

    A user row is visible to the user themself, to the organisations they belong
    to (while in that organisation's context), and transiently to a sign-in
    lookup by exact email. Invitation-based access is added by the members app.
    """

    dependencies = [
        ("accounts", "0001_initial"),
        ("tenancy", "0002_rls"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE app_user ENABLE ROW LEVEL SECURITY;
                ALTER TABLE app_user FORCE ROW LEVEL SECURITY;

                CREATE POLICY self_read ON app_user FOR SELECT
                    USING (id = current_app_user_id());
                CREATE POLICY self_update ON app_user FOR UPDATE
                    USING (id = current_app_user_id()) WITH CHECK (id = current_app_user_id());

                CREATE POLICY org_member_read ON app_user FOR SELECT USING (
                    EXISTS (SELECT 1 FROM org_membership m
                            WHERE m.user_id = app_user.id
                              AND m.organisation_id = current_org_id()));

                CREATE POLICY auth_lookup_read ON app_user FOR SELECT
                    USING (email = nullif(current_setting('app.auth_email', true), ''));
                CREATE POLICY auth_lookup_update ON app_user FOR UPDATE
                    USING (email = nullif(current_setting('app.auth_email', true), ''))
                    WITH CHECK (email = nullif(current_setting('app.auth_email', true), ''));

                -- Organisation admins create invited accounts, only within an org context.
                CREATE POLICY org_invite_insert ON app_user FOR INSERT TO app_rw
                    WITH CHECK (current_org_id() IS NOT NULL AND status = 'invited');

                CREATE POLICY platform_all ON app_user TO app_platform
                    USING (true) WITH CHECK (true);
            """,
            reverse_sql="""
                DROP POLICY platform_all ON app_user;
                DROP POLICY org_invite_insert ON app_user;
                DROP POLICY auth_lookup_update ON app_user;
                DROP POLICY auth_lookup_read ON app_user;
                DROP POLICY org_member_read ON app_user;
                DROP POLICY self_update ON app_user;
                DROP POLICY self_read ON app_user;
                ALTER TABLE app_user NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE app_user DISABLE ROW LEVEL SECURITY;
            """,
        ),
    ]
