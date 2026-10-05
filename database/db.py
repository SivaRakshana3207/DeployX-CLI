import sqlite3
from config import DATABASE_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    framework TEXT NOT NULL,
    build_command TEXT,
    output_directory TEXT,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    github_url TEXT,
    vercel_url TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    connection = get_connection()
    try:
        connection.executescript(SCHEMA)
        connection.commit()
    finally:
        connection.close()


def fetch_all(query, params=()):
    connection = get_connection()
    try:
        rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def fetch_one(query, params=()):
    connection = get_connection()
    try:
        row = connection.execute(query, params).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def execute(query, params=()):
    connection = get_connection()
    try:
        cursor = connection.execute(query, params)
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()
