"""Settings shared by all the tests."""

from argon2 import PasswordHasher

from synaptane.core import security

# Passwords: the production cost of argon2 (64 MiB, 3 passes, 4 lanes) is there to slow down
# an attacker, not the suite, which creates and checks accounts in hundreds of tests, several
# at once. Same algorithm and format, minimal cost. Commands run as subprocesses keep the
# production cost.
security._hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
security._DUMMY_HASH = security._hasher.hash("synaptane-dummy-password")
