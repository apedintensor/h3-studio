"""Local password hashes only; no registration, secrets in arguments, or cloud APIs."""
import secrets
import re
from contextlib import closing
import sqlite3
import time

import bcrypt

USERS = frozenset(("superdan", "supervan"))
MIN_PASSWORD_CHARACTERS = 12
MAX_PASSWORD_BYTES = 72
_DUMMY_HASH = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt(rounds=12))


def initialize_auth_schema(connection):
    connection.execute("CREATE TABLE IF NOT EXISTS auth_passwords(username TEXT PRIMARY KEY, password_hash BLOB NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS auth_login_limits(source_hash TEXT PRIMARY KEY, window_start REAL NOT NULL, attempts INTEGER NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS auth_sessions(token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, auth_mode TEXT NOT NULL DEFAULT 'username-test')")
    if 'auth_mode' not in {row[1] for row in connection.execute('PRAGMA table_info(auth_sessions)')}:
        connection.execute("ALTER TABLE auth_sessions ADD COLUMN auth_mode TEXT NOT NULL DEFAULT 'username-test'")


def passwords_ready(connection):
    try:
        rows = connection.execute('SELECT username,password_hash FROM auth_passwords WHERE disabled=0').fetchall()
    except sqlite3.OperationalError:
        return False
    return USERS.issubset({row[0] for row in rows if isinstance(row[1], bytes)
        and re.fullmatch(rb'\$2b\$12\$[./A-Za-z0-9]{21}[.Oeu][./A-Za-z0-9]{30}[.CGKOSWaeimquy26]', row[1])})


def verify_password(connection, username, password):
    valid_input = isinstance(username, str) and isinstance(password, str)
    try:
        encoded = password.encode('utf-8') if valid_input else b''
    except UnicodeError:
        encoded = b''
    valid_input = valid_input and 0 < len(encoded) <= MAX_PASSWORD_BYTES
    row = connection.execute('SELECT password_hash, disabled FROM auth_passwords WHERE username=?',
        (username if isinstance(username, str) else '',)).fetchone()
    eligible = valid_input and username in USERS and row is not None and not row[1]
    stored = bytes(row[0]) if eligible else _DUMMY_HASH
    # Every syntactically valid or invalid account attempt pays a bcrypt check;
    # overlong inputs are rejected rather than silently truncated by bcrypt.
    try:
        matched = bcrypt.checkpw(encoded if valid_input else b'invalid-password', stored)
    except ValueError:
        bcrypt.checkpw(b'invalid-password', _DUMMY_HASH)
        matched = False
    return bool(eligible and matched)


def set_password(database, username, password):
    """Called by the interactive operator tool (tests use fake passwords)."""
    if username not in USERS:
        raise ValueError('Unsupported account')
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_CHARACTERS:
        raise ValueError('Password must contain at least 12 characters')
    encoded = password.encode('utf-8')
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError('Password must not exceed 72 UTF-8 bytes')
    password_hash = bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=12))
    with closing(sqlite3.connect(database, timeout=20)) as connection:
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            initialize_auth_schema(connection)
            connection.execute('INSERT INTO auth_passwords(username,password_hash,disabled,updated) VALUES(?,?,0,?) ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash, disabled=0, updated=excluded.updated',
                (username, password_hash, time.time()))
            connection.execute('DELETE FROM auth_sessions WHERE username=?', (username,))
