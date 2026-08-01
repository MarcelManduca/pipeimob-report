import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL")

engine = None
SessionLocal = None

def init_db(database_url: str):
    global engine, SessionLocal
    if not database_url:
        raise ValueError("Database URL cannot be empty")
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

app_env = os.getenv("APP_ENV", "development").lower()
if app_env in ["production", "staging"] and not DATABASE_URL:
    raise RuntimeError("Server startup error: DATABASE_URL environment variable is required in production and staging environments.")


if DATABASE_URL:
    init_db(DATABASE_URL)


Base = declarative_base()

def get_db():
    global SessionLocal
    if not SessionLocal:
        raise RuntimeError("DATABASE_URL is not set.")
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
