from django.contrib.auth import get_user_model
from django.db import transaction

from audit.services import record as audit
from platform_core.db import db_context

from .models import Organisation, OrgMembership, RoleAssignment
from .roles import Role


def provision_organisation(*, slug: str, legal_name: str, admin_email: str,
                           admin_name: str = "", using: str = "platform"):
    """Create an organisation and its first administrator (Platform Owner action).

    Runs on the ``platform`` connection (role ``app_platform``). The admin
    account is created as ``invited`` with no usable password; the invitation
    flow (increment 6.2) lets them set a password and enrol a second factor.
    """
    User = get_user_model()
    with transaction.atomic(using=using):
        org = Organisation(slug=slug, legal_name=legal_name)
        org.full_clean()
        org.save(using=using)
        user = User.objects.db_manager(using).filter(
            email=User.objects.normalize_email(admin_email)).first()
        if user is None:
            user = User.objects.db_manager(using).create_user(
                admin_email, full_name=admin_name, status=User.Status.INVITED)
        with db_context(org_id=org.id, using=using):
            membership = OrgMembership.objects.db_manager(using).create(
                organisation=org, user=user)
            RoleAssignment.objects.db_manager(using).create(
                organisation=org, membership=membership, role=Role.ORG_ADMIN)
            audit("organisation.provisioned", organisation_id=org.id, using=using,
                  object_type="organisation", object_id=org.id,
                  details={"slug": slug, "first_admin_user_id": str(user.id)})
    return org, user
