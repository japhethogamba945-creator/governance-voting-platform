from django.db import migrations


class Migration(migrations.Migration):
    initial = True
    dependencies: list = []

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE FUNCTION current_org_id() RETURNS uuid
                    LANGUAGE sql STABLE PARALLEL SAFE
                    AS $$ SELECT nullif(current_setting('app.org_id', true), '')::uuid $$;
                CREATE FUNCTION current_app_user_id() RETURNS uuid
                    LANGUAGE sql STABLE PARALLEL SAFE
                    AS $$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;
            """,
            reverse_sql="""
                DROP FUNCTION current_app_user_id();
                DROP FUNCTION current_org_id();
            """,
        ),
    ]
