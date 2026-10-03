"""Provision one private DB/least-privilege role from existing protected files.

Run only in the db-init service. No password generation, default credentials,
dotenv, secret copying, logging of SQL or unattended password rotation occurs.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

ADMIN_FILE = Path("/run/secrets/db_admin_password")
DSN_FILE = Path("/run/secrets/app_database_url")


class BootstrapError(Exception):
    pass


def read_secret(path):
    try:
        info = path.lstat()
        if (not path.is_absolute() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > 16384 or (os.name == "posix" and info.st_mode & 0o027)):
            raise ValueError
        value = path.read_text(encoding="utf-8").removesuffix("\n")
        if not value or any(char in value for char in "\r\n\x00"):
            raise ValueError
        return value
    except Exception:
        raise BootstrapError("A protected database secret file is missing or invalid") from None


def connection_settings(dsn):
    try:
        url = make_url(dsn)
        if (url.drivername != "postgresql+psycopg" or url.host != "db" or url.port not in (None, 5432)
                or url.username != "sixnine_app" or url.database != "sixnine"
                or not url.password or len(url.password) < 32 or len(url.password) > 256
                or any(c in url.password for c in "\r\n\x00")
                or dict(url.query) not in ({}, {"sslmode": "disable"})):
            raise ValueError
        return dict(host="db", port=5432, dbname="sixnine", user="sixnine_app", password=url.password,
                    sslmode="disable", connect_timeout=10, autocommit=True)
    except Exception:
        raise BootstrapError("App DSN must select the private sixnine database and non-admin role") from None


def provision(admin_password, app_settings, *, connector=psycopg.connect):
    if len(admin_password) < 32 or len(admin_password) > 256:
        raise BootstrapError("The existing database admin secret does not meet the length policy")
    if admin_password == app_settings["password"]:
        raise BootstrapError("Administrator and app credentials must be independent")
    role, database = app_settings["user"], app_settings["dbname"]
    try:
        admin = dict(host="db", port=5432, user="postgres", password=admin_password,
                     dbname="postgres", sslmode="disable", connect_timeout=10, autocommit=True)
        with connector(**admin) as connection:
            major = int(connection.execute("SHOW server_version_num").fetchone()[0])
            if not 170000 <= major < 180000:
                raise BootstrapError("This deployment package requires reviewed PostgreSQL 17")
            connection.execute("SELECT pg_advisory_lock(696969)")
            flags = connection.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin, rolconnlimit "
                                       "FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            if flags is None:
                # SQL literal escaping is required for this DDL, which does not
                # accept a bind parameter in the password position. Never log it.
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 32 PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(app_settings["password"])))
            elif any(flags[:5]) or not flags[5] or not 1 <= flags[6] <= 32:
                raise BootstrapError("Existing app role has excessive privileges; refusing to reuse or alter it")
            memberships = connection.execute("SELECT 1 FROM pg_auth_members WHERE member = "
                                             "(SELECT oid FROM pg_roles WHERE rolname = %s) LIMIT 1", (role,)).fetchone()
            if memberships is not None:
                raise BootstrapError("Existing app role has inherited memberships; refusing to reuse it")
            owner = connection.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
                                       (database,)).fetchone()
            if owner is None:
                connection.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(role)))
            elif owner[0] != role:
                raise BootstrapError("Existing database has another owner; refusing to change it")
            connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
        with connector(**app_settings) as connection:
            identity = connection.execute("SELECT current_user, current_database()").fetchone()
            if tuple(identity) != (role, database):
                raise BootstrapError("Database authentication selected an unexpected identity")
            connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            connection.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(sql.Identifier(role)))
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError("Database bootstrap failed; verify secret references, network and prior role state") from None


def main():
    try:
        app_settings = connection_settings(read_secret(DSN_FILE))
        provision(read_secret(ADMIN_FILE), app_settings)
        print("Private application database is ready; no account password or cloud resources were created")
        return 0
    except BootstrapError:
        # No driver exceptions, DSNs, SQL statements or passwords reach logs.
        print("Database bootstrap refused; inspect protected configuration and role state", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
