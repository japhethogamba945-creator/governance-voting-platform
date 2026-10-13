from django.contrib.auth.hashers import Argon2PasswordHasher


class FastArgon2Hasher(Argon2PasswordHasher):
    """Cheap parameters so the test suite stays fast. Never used outside tests."""

    time_cost = 1
    memory_cost = 1024
    parallelism = 1
