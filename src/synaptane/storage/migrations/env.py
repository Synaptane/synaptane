from alembic import context
from sqlalchemy import create_engine

from synaptane.storage.models import Base

url = context.config.get_main_option("sqlalchemy.url")
assert url is not None
engine = create_engine(url)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
