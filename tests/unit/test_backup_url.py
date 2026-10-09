from synaptane.services import backup


def test_password_stays_out_of_the_command_line() -> None:
    dsn, env = backup._libpq("postgresql+psycopg://u:s%40cret@db:5432/base?sslmode=require")
    assert dsn == "postgresql://u@db:5432/base?sslmode=require"
    assert env["PGPASSWORD"] == "s@cret"
