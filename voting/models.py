import uuid

from django.db import models
from django.db.models.functions import Length
from django.db.models.lookups import Exact

from .encoding import CIPHERTEXT_BYTES


class Ballot(models.Model):
    """A sealed ballot in the ballot box.

    Deliberately has **no** reference to a member or entitlement and **no**
    timestamp (docs/design/BLUEPRINT.md §4). Only the app_voting role may insert
    and nobody may update or delete (grants plus a trigger); the web role cannot
    even read this table.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    election = models.ForeignKey("elections.Election", on_delete=models.PROTECT,
                                 related_name="+")
    ciphertext = models.BinaryField()
    receipt_commitment = models.BinaryField()

    class Meta:
        db_table = "ballot"
        constraints = [
            models.UniqueConstraint(fields=["election", "receipt_commitment"],
                                    name="ballot_receipt_uniq"),
            models.CheckConstraint(condition=Exact(Length("ciphertext"), CIPHERTEXT_BYTES),
                                   name="ballot_ciphertext_fixed_size"),
            models.CheckConstraint(condition=Exact(Length("receipt_commitment"), 32),
                                   name="ballot_receipt_commitment_size"),
        ]

