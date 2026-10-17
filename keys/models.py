import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from platform_core.db import TenantManager


class KeyholderKey(models.Model):
    """An official's key pair for holding election key shares in one organisation.

    The private key is stored only encrypted under the official's passphrase
    (Argon2id); the server cannot open it without them.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                             related_name="+")
    public_key = models.BinaryField()
    salt = models.BinaryField()
    encrypted_private_key = models.BinaryField()
    kdf = models.CharField(max_length=16)
    created_at = models.DateTimeField(default=timezone.now)
    retired_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "keyholder_key"
        constraints = [
            models.UniqueConstraint(fields=["organisation", "user"],
                                    condition=Q(retired_at__isnull=True),
                                    name="keyholder_key_one_active"),
            models.UniqueConstraint(fields=["organisation", "id"],
                                    name="keyholder_key_org_id_uniq"),
        ]


class KeyShare(models.Model):
    """One holder's encrypted share of an election's private key.

    ``keyholder_key`` is empty for the sealed offline backup share, which is
    encrypted under a one-time backup code instead.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    election = models.ForeignKey("elections.Election", on_delete=models.PROTECT,
                                 related_name="key_shares")
    share_index = models.PositiveSmallIntegerField()
    keyholder_key = models.ForeignKey(KeyholderKey, on_delete=models.PROTECT, null=True,
                                      blank=True, related_name="+")
    holder_label = models.CharField(max_length=200)
    # Public key that pieces are sealed to while counting (the keyholder's own key,
    # or the key derived from the backup code). Empty only for shares made before 6.7.
    recipient_public_key = models.BinaryField(null=True, blank=True)
    encrypted_bundle = models.BinaryField()
    bundle_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "key_share"
        ordering = ["share_index"]
        constraints = [
            models.UniqueConstraint(fields=["election", "share_index"],
                                    name="key_share_index_uniq"),
            models.UniqueConstraint(fields=["election", "keyholder_key"],
                                    condition=Q(keyholder_key__isnull=False),
                                    name="key_share_one_per_holder"),
        ]
