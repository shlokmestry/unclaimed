# Synchronous engine/session, used by run.py (table creation) and the
# ingestion scripts, which are simple sequential scripts with no need for
# async I/O.

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.config import DATABASE_URL

engine = create_engine(DATABASE_URL, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
