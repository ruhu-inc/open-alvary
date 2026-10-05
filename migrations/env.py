import os

from alembic import context

from api.store import engine_for, metadata

url = os.getenv("DATABASE_URL", context.config.get_main_option("sqlalchemy.url"))
if context.is_offline_mode():
    context.configure(url=url, target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with engine_for(url).connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
