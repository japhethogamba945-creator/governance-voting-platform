import hashlib
import secrets
import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django_otp.models import Device


class UserManager(BaseUserManager):
    def normalize_email(self, email):  # case-insensitive identity
        return (email or "").strip().lower()

    def get_by_natural_key(self, username):
        return self.get(email=self.normalize_email(username))

    def create_user(self, email, password=None, **extra):
        user = self.model(email=self.normalize_email(email), **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user


class User(AbstractBaseUser):
    class Status(models.TextChoices):
        INVITED = "invited"
        ACTIVE = "active"
        LOCKED = "locked"
        DISABLED = "disabled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(max_length=254, unique=True)  # always stored lower-case
    full_name = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.INVITED)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta:
        db_table = "app_user"
        constraints = [
            models.CheckConstraint(condition=models.Q(email=Lower("email")),
                                   name="app_user_email_lowercase"),
            models.CheckConstraint(
                condition=models.Q(status__in=["invited", "active", "locked", "disabled"]),
                name="app_user_status_valid",
            ),
        ]

    @property
    def is_active(self) -> bool:  # consulted by ModelBackend
        return self.status == self.Status.ACTIVE

    def __str__(self) -> str:
        return self.email


RECOVERY_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32


def _normalise_code(code: str) -> str:
    return "".join(code.split()).replace("-", "").upper()


def _hash_code(code: str) -> str:
    # Codes are 50-bit random values and attempts are throttled, so a fast
    # hash is adequate; the point is that the database never holds them.
    return hashlib.sha256(_normalise_code(code).encode()).hexdigest()


class RecoveryCodeDevice(Device):
    """Single-use recovery codes, stored hashed (django-otp's static device stores plaintext)."""

    class Meta:
        db_table = "accounts_recovery_code_device"

    def generate_codes(self, count: int = 10) -> list[str]:
        self.codes.all().delete()
        plain = []
        for _ in range(count):
            raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(10))
            plain.append(f"{raw[:5]}-{raw[5:]}")
        RecoveryCode.objects.bulk_create(
            RecoveryCode(device=self, code_hash=_hash_code(c)) for c in plain
        )
        return plain

    def verify_token(self, token) -> bool:
        deleted, _ = self.codes.filter(code_hash=_hash_code(str(token))).delete()
        return deleted > 0


class RecoveryCode(models.Model):
    device = models.ForeignKey(RecoveryCodeDevice, on_delete=models.CASCADE, related_name="codes")
    code_hash = models.CharField(max_length=64)

    class Meta:
        db_table = "accounts_recovery_code"
        constraints = [
            models.UniqueConstraint(fields=["device", "code_hash"], name="recovery_code_uniq")
        ]


class Throttle(models.Model):
    """Failure counters for login/MFA rate limiting (keys are HMACs, never raw emails)."""

    key = models.CharField(primary_key=True, max_length=100)
    failures = models.PositiveIntegerField(default=0)
    window_start = models.DateTimeField(default=timezone.now)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_throttle"
