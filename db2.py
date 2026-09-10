"""Compatibility alias to the shared database configuration."""
from db import get_engine

def is_postgres():
    return get_engine().dialect.name == 'postgresql'
