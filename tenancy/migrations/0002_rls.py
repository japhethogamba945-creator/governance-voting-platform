from django.db import migrations

from platform_core.rls import composite_fk, tenant_rls


class Migration(migrations.Migration):
    dependencies = [
        ("platform_core", "0001_rls_functions"),
        ("tenancy", "0001_initial"),
    ]

    operations = [
        # organisation is the tenant itself: keyed on id rather than organisation_id.
        migrations.RunSQL(
            sql="""
                ALTER TABLE organisation ENABLE ROW LEVEL SECURITY;
                ALTER TABLE organisation FORCE ROW LEVEL SECURITY;
                CREATE POLICY member_read ON organisation FOR SELECT USING (
                    id = current_org_id()
                    OR EXISTS (SELECT 1 FROM org_membership m
                               WHERE m.organisation_id = organisation.id
                                 AND m.user_id = current_app_user_id()));
                CREATE POLICY tenant_update ON organisation FOR UPDATE
                    USING (id = current_org_id()) WITH CHECK (id = current_org_id());
                CREATE POLICY platform_all ON organisation TO app_platform
                    USING (true) WITH CHECK (true);
            """,
            reverse_sql="""
                DROP POLICY platform_all ON organisation;
                DROP POLICY tenant_update ON organisation;
                DROP POLICY member_read ON organisation;
                ALTER TABLE organisation NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE organisation DISABLE ROW LEVEL SECURITY;
            """,
        ),
        tenant_rls("org_membership"),
        # A user may always read their own memberships (to list "my organisations"
        # and resolve /o/<slug>/), but may only write within the current organisation.
        migrations.RunSQL(
            sql="CREATE POLICY own_membership_read ON org_membership FOR SELECT "
                "USING (user_id = current_app_user_id());",
            reverse_sql="DROP POLICY own_membership_read ON org_membership;",
        ),
        tenant_rls("role_assignment"),
        composite_fk("role_assignment", "role_assignment_membership_same_org",
                     ["organisation_id", "membership_id"],
                     "org_membership", ["organisation_id", "id"]),
    ]
