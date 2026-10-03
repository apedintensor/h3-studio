"""Shared SQL authentication, scoped machine clients, and revocable sessions."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import secrets
import time

import bcrypt
from sqlalchemy import Column, Float, Integer, MetaData, String, Table, Text, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
accounts = Table("platform_accounts", metadata,
    Column("tenant", String(200), primary_key=True),
    Column("username", String(80), primary_key=True), Column("password_hash", Text, nullable=False),
    Column("disabled", Integer, nullable=False, default=0), Column("updated", Float, nullable=False))
sessions = Table("platform_sessions", metadata,
    Column("tenant", String(200), primary_key=True),
    Column("token_hash", String(64), primary_key=True), Column("username", String(80), nullable=False),
    Column("auth_mode", String(20), nullable=False), Column("created", Float, nullable=False),
    Column("expires", Float, nullable=False), Column("password_version", Float, nullable=False))
login_limits = Table("platform_login_limits", metadata,
    Column("tenant", String(200), primary_key=True),
    Column("source_hash", String(64), primary_key=True), Column("window_start", Float, nullable=False),
    Column("attempts", Integer, nullable=False))
clients = Table("platform_service_clients", metadata,
    Column("tenant", String(200), primary_key=True),
    Column("id", String(80), primary_key=True), Column("token_hash", String(64), unique=True, nullable=False),
    Column("owner", String(80), nullable=False), Column("projects", Text, nullable=False),
    Column("scopes", Text, nullable=False), Column("disabled", Integer, nullable=False, default=0))

USERS = frozenset({"superdan", "supervan"})
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
PASSWORD_HASH = re.compile(r"^\$2[aby]\$12\$[./A-Za-z0-9]{53}$")
_DUMMY = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt(rounds=12))


class AuthenticationError(Exception):
    pass


class LoginLimited(AuthenticationError):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__("登录尝试过多，请稍后重试")


@dataclass(frozen=True)
class Principal:
    owner: str
    actor_id: str
    machine: bool = False
    project_ids: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()

    def allows(self, project_id: str, scope: str) -> bool:
        return not self.machine or (project_id in self.project_ids and scope in self.scopes)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Auth:
    def __init__(self, engine, *, tenant="sixnine", mode="password", session_seconds=43200, clock=time.time):
        self.engine = engine
        self.tenant = tenant
        self.mode = mode
        self.session_seconds = session_seconds
        self.clock = clock
        metadata.create_all(engine)

    def ready(self, connection=None):
        if self.mode == "local-test":
            return True
        if connection is None:
            with self.engine.connect() as conn:
                return self.ready(conn)
        rows = connection.execute(select(accounts.c.username, accounts.c.password_hash, accounts.c.disabled).where(
            accounts.c.tenant == self.tenant)).all()
        present = {name for name, encoded, _ in rows if PASSWORD_HASH.fullmatch(encoded)}
        # Disabling one account must not log out the other configured account.
        return USERS.issubset(present) and any(not disabled for _, _, disabled in rows)

    def set_password(self, username: str, password: str):
        if username not in USERS:
            raise ValueError("Unsupported account")
        if not isinstance(password, str) or len(password) < 12 or len(password.encode("utf-8")) > 72:
            raise ValueError("Password must have at least 12 characters and at most 72 UTF-8 bytes")
        encoded = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
        with self.engine.begin() as conn:
            existing = conn.execute(select(accounts.c.updated).where(accounts.c.tenant == self.tenant,
                accounts.c.username == username).with_for_update()).first()
            changed_at = max(self.clock(), existing[0] + .000001) if existing else self.clock()
            values = {"password_hash": encoded, "disabled": 0, "updated": changed_at}
            if existing:
                conn.execute(update(accounts).where(accounts.c.tenant == self.tenant, accounts.c.username == username).values(**values))
            else:
                conn.execute(insert(accounts).values(tenant=self.tenant, username=username, **values))
            conn.execute(delete(sessions).where(sessions.c.tenant == self.tenant, sessions.c.username == username))

    def reserve_login(self, source: str):
        key, now = digest(source), self.clock()
        # A unique row followed by a conditional UPDATE also serializes writers
        # when two API processes see the first attempt at the same time.
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(login_limits).values(tenant=self.tenant, source_hash=key, window_start=now, attempts=0))
        except IntegrityError:
            pass
        with self.engine.begin() as conn:
            conn.execute(update(login_limits).where(login_limits.c.tenant == self.tenant, login_limits.c.source_hash == key,
                login_limits.c.window_start <= now - 300).values(window_start=now, attempts=0))
            changed = conn.execute(update(login_limits).where(login_limits.c.tenant == self.tenant, login_limits.c.source_hash == key,
                login_limits.c.attempts < 5).values(attempts=login_limits.c.attempts + 1))
            if changed.rowcount != 1:
                start = conn.execute(select(login_limits.c.window_start).where(login_limits.c.tenant == self.tenant, login_limits.c.source_hash == key)).scalar_one()
                raise LoginLimited(max(1, int(start + 300 - now)))

    def login(self, username, password, source="local"):
        self.reserve_login(source)
        if self.mode == "local-test":
            if username not in USERS:
                raise AuthenticationError("用户名或密码不正确")
            version = 0.0
        else:
            with self.engine.connect() as conn:
                row = conn.execute(select(accounts).where(accounts.c.tenant == self.tenant,
                    accounts.c.username == (username if isinstance(username, str) else ""))).mappings().first()
            valid = isinstance(password, str) and 0 < len(password.encode("utf-8")) <= 72
            eligible = username in USERS and row is not None and not row["disabled"] and self.ready()
            hashed = row["password_hash"].encode("ascii") if eligible else _DUMMY
            matched = bcrypt.checkpw(password.encode("utf-8") if valid else b"invalid", hashed)
            if not valid or not eligible or not matched:
                raise AuthenticationError("用户名或密码不正确")
            version = row["updated"]
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.engine.begin() as conn:
            if self.mode == "password":
                current = conn.execute(select(accounts.c.updated, accounts.c.disabled).where(accounts.c.tenant == self.tenant,
                    accounts.c.username == username).with_for_update()).first()
                if not current or current[1] or current[0] != version:
                    raise AuthenticationError("账户状态已改变，请重新登录")
            conn.execute(insert(sessions).values(tenant=self.tenant, token_hash=digest(token), username=username,
                auth_mode=self.mode, created=now, expires=now+self.session_seconds, password_version=version))
        return token

    def session(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        with self.engine.connect() as conn:
            row = conn.execute(select(sessions).where(sessions.c.tenant == self.tenant, sessions.c.token_hash == digest(token),
                sessions.c.expires > self.clock(), sessions.c.auth_mode == self.mode)).mappings().first()
            if not row:
                return None
            if self.mode == "password":
                account = conn.execute(select(accounts).where(accounts.c.tenant == self.tenant, accounts.c.username == row["username"])).mappings().first()
                if not account or account["disabled"] or account["updated"] != row["password_version"] or not self.ready(conn):
                    return None
        return Principal(row["username"], "browser:"+row["username"])

    def logout(self, token):
        if isinstance(token, str) and TOKEN.fullmatch(token):
            with self.engine.begin() as conn:
                conn.execute(delete(sessions).where(sessions.c.tenant == self.tenant, sessions.c.token_hash == digest(token)))

    def register_client(self, client_id, token, owner, project_ids, scopes):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", client_id) or owner not in USERS:
            raise ValueError("Invalid client identity")
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ValueError("Client token must be a high entropy URL-safe value")
        allowed = {"projects:read", "assets:read", "assets:write", "jobs:read", "jobs:write"}
        if (not isinstance(project_ids, (list, tuple)) or not 1 <= len(project_ids) <= 4096
                or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", x) for x in project_ids)
                or not isinstance(scopes, (list, tuple)) or any(not isinstance(s, str) or s not in allowed for s in scopes)):
            raise ValueError("Client must have explicit projects and scopes")
        with self.engine.begin() as conn:
            # Rotation changes the hash atomically and invalidates the old token.
            values = dict(token_hash=digest(token), owner=owner, projects=json.dumps(project_ids), scopes=json.dumps(scopes), disabled=0)
            if conn.execute(select(clients.c.id).where(clients.c.tenant == self.tenant, clients.c.id == client_id)).first():
                conn.execute(update(clients).where(clients.c.tenant == self.tenant, clients.c.id == client_id).values(**values))
            else:
                conn.execute(insert(clients).values(tenant=self.tenant, id=client_id, **values))

    def bearer(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        with self.engine.connect() as conn:
            row = conn.execute(select(clients).where(clients.c.tenant == self.tenant,
                clients.c.token_hash == digest(token), clients.c.disabled == 0)).mappings().first()
            if row and self.mode == "password":
                active = conn.execute(select(accounts.c.username).where(accounts.c.tenant == self.tenant,
                    accounts.c.username == row["owner"], accounts.c.disabled == 0)).first()
                if not active or not self.ready(conn):
                    return None
        return Principal(row["owner"], "client:"+row["id"], True, tuple(json.loads(row["projects"])), tuple(json.loads(row["scopes"]))) if row else None
