from django.db import migrations

from platform_core.rls import composite_fk, tenant_rls

INVITE_TOKEN = "nullif(current_setting('app.invite_token_hash', true), '')"


class Migration(migrations.Migration):
    dependencies = [
        ("members", "0001_initial"),
        ("accounts", "0002_app_user_rls"),
    ]

    operations = [
        tenant_rls("member"),
        tenant_rls("invitation"),
        tenant_rls("member_import_batch"),
        composite_fk("invitation", "invitation_member_same_org",
                     ["organisation_id", "member_id"], "member", ["organisation_id", "id"]),
        # Accepting an invitation happens before sign-in and outside any
        # organisation context. Presenting the token (app.invite_token_hash) makes
        # exactly that invitation, its user, member and organisation readable.
        migrations.RunSQL(
            sql=f"""
                CREATE POLICY token_read ON invitation FOR SELECT
                    USING (token_hash = {INVITE_TOKEN});
                CREATE POLICY token_accept ON invitation FOR UPDATE
                    USING (token_hash = {INVITE_TOKEN}) WITH CHECK (token_hash = {INVITE_TOKEN});

                CREATE POLICY invite_read ON app_user FOR SELECT USING (
                    id IN (SELECT user_id FROM invitation WHERE token_hash = {INVITE_TOKEN}));
                CREATE POLICY invite_update ON app_user FOR UPDATE USING (
                    id IN (SELECT user_id FROM invitation WHERE token_hash = {INVITE_TOKEN}))
                    WITH CHECK (
                    id IN (SELECT user_id FROM invitation WHERE token_hash = {INVITE_TOKEN}));

                CREATE POLICY invite_read ON member FOR SELECT USING (
                    id IN (SELECT member_id FROM invitation WHERE token_hash = {INVITE_TOKEN}));
                CREATE POLICY invite_read ON organisation FOR SELECT USING (
                    id IN (SELECT organisation_id FROM invitation
                           WHERE token_hash = {INVITE_TOKEN}));

                -- A user may read their own role assignments (to list "my organisations").
                CREATE POLICY own_role_read ON role_assignment FOR SELECT USING (
                    membership_id IN (SELECT id FROM org_membership
                                      WHERE user_id = current_app_user_id()));
            """,
            reverse_sql="""
                DROP POLICY own_role_read ON role_assignment;
                DROP POLICY invite_read ON organisation;
                DROP POLICY invite_read ON member;
                DROP POLICY invite_update ON app_user;
                DROP POLICY invite_read ON app_user;
                DROP POLICY token_accept ON invitation;
                DROP POLICY token_read ON invitation;
            """,
        ),
    ]
