"""Provision both accounts on the destination host before publishing HTTPS.

Run: python tools/manage_users.py --data-dir /var/lib/h3-studio
Change one account: add --username superdan (or supervan).
Passwords are entered with getpass, never via arguments/environment or logs.
The database must remain private to the service user; changing a password
revokes that account's existing sessions. No public registration/reset API exists.
"""
import argparse
from contextlib import closing
import getpass
import os
from pathlib import Path
import sqlite3
import sys
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from password_auth import USERS, passwords_ready, set_password


def main():
    parser = argparse.ArgumentParser(description='Interactively provision H3 Studio account passwords')
    parser.add_argument('--data-dir', default=os.environ.get('H3_STUDIO_DATA'), required=not bool(os.environ.get('H3_STUDIO_DATA')))
    parser.add_argument('--username', choices=sorted(USERS))
    args = parser.parse_args()
    directory = Path(args.data_dir).resolve()
    if not directory.is_dir():
        parser.error('Data directory must already exist')
    if not sys.stdin.isatty():
        parser.error('Interactive terminal required; password input through a pipe is forbidden')
    database = directory / 'studio.sqlite3'
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            for username in ([args.username] if args.username else sorted(USERS)):
                password = getpass.getpass(f'{username} new password (12+ characters, at most 72 UTF-8 bytes): ')
                confirmation = getpass.getpass('Confirm password: ')
                if password != confirmation:
                    raise ValueError('Passwords did not match; this account was not changed')
                set_password(database, username, password)
                del password, confirmation
                print(f'{username}: password saved; previous sessions revoked')
        with closing(sqlite3.connect(database)) as connection:
            print('Both accounts are ready' if passwords_ready(connection) else 'Not ready: provision both accounts before public access')
    except (ValueError, getpass.GetPassWarning) as error:
        print(str(error), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('Password setup cancelled', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
