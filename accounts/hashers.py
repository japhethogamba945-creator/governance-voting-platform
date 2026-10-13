from django.contrib.auth.hashers import Argon2PasswordHasher


class Argon2idHasher(Argon2PasswordHasher):
    """Argon2id at or above the OWASP baseline (64 MiB, t=3, p=1).

    Tune on production hardware for roughly 250 ms per hash; Django rehashes
    automatically at login when these values change.
    """

    time_cost = 3
    memory_cost = 65536
    parallelism = 1
