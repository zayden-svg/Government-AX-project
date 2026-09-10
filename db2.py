# db.py
import os
from sqlalchemy import create_engine

_engine = None


def _resolve_database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    try:
        import streamlit as st
        return str(st.secrets.get("DATABASE_URL", "")).strip()
    except Exception:
        return ""


def get_engine():
    global _engine
    if _engine is not None:
        return _engine

    db_url = _resolve_database_url()
    if not db_url:
        db_url = "sqlite:///gov_tracker.db"

    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif db_url.startswith("postgresql://") and "+psycopg2" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

    _engine = create_engine(db_url, pool_pre_ping=True)
    return _engine


def is_postgres():
    return "postgresql" in get_engine().url.drivername
