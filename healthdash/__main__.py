"""healthdash command line.

  python3 -m healthdash service install|status|uninstall [--port] [--lan] [--title]   (macOS)
  python3 -m healthdash user list|add|passwd|remove [NAME] --db DB    (accounts, from a terminal)
  python3 -m healthdash wipe    --db DB --files DIR [--yes]           (empty all records; keeps accounts)
  python3 -m healthdash check   [--ingest]                             (is everything installed?)
  python3 -m healthdash demo    [--port 9380]                          (made-up sample data)
  python3 -m healthdash serve   --db DB --port 9380 --files DIR --import-from INGEST_HOST_IP --title "Our Health"
  python3 -m healthdash ingest  [--source DIR] [--pi URL] [--dry-run]      (run where the files are)
  python3 -m healthdash organize [--source DIR] [--dest DIR] [--apply]     (run where the files are)
"""

import argparse
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog='healthdash')
    sub = ap.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('serve', help='run the dashboard')
    s.add_argument('--db', required=True)
    s.add_argument('--port', type=int, default=9380)
    s.add_argument('--files', required=True, help='where copies of the source files are kept')
    s.add_argument('--import-from', action='append', default=['127.0.0.1'],
                   help='address allowed to import/upload (repeatable)')
    s.add_argument('--title', default=None, help='name shown at the top of the page')
    s.add_argument('--host', default='0.0.0.0', help='127.0.0.1 = only this computer')

    v = sub.add_parser('service', help='macOS: run the dashboard in the background, starting at login')
    v.add_argument('action', choices=['install', 'status', 'uninstall'])
    v.add_argument('--port', type=int, default=9380)
    v.add_argument('--lan', action='store_true', help='let other devices on your network open it')
    v.add_argument('--title', default=None)

    u = sub.add_parser('user', help='manage sign-in accounts from a terminal')
    u.add_argument('action', choices=['list', 'add', 'passwd', 'remove'])
    u.add_argument('name', nargs='?')
    u.add_argument('--db', required=True, help='the dashboard database file')

    w = sub.add_parser('wipe', help='empty every record and stored file to start over (keeps accounts)')
    w.add_argument('--db', required=True, help='the dashboard database file')
    w.add_argument('--files', required=True, help='the dashboard files folder')
    w.add_argument('--yes', action='store_true', help='do not ask to type WIPE')
    c = sub.add_parser('check', help='check this machine has everything healthdash needs')
    c.add_argument('--ingest', action='store_true', help='check the records machine instead of the server')

    d = sub.add_parser('demo', help='run on made-up sample data in a throwaway database')
    d.add_argument('--port', type=int, default=9380)
    d.add_argument('--host', default='127.0.0.1', help='use 0.0.0.0 to open it to the LAN')

    from . import config
    i = sub.add_parser('ingest', help='extract new files and send them to the dashboard')
    i.add_argument('--source', default=config.SOURCE)
    i.add_argument('--pi', default=config.PI_URL)
    i.add_argument('--dry-run', action='store_true')

    o = sub.add_parser('organize', help='build the sorted, renamed copy of the records (hard links)')
    o.add_argument('--source', default=config.SOURCE)
    o.add_argument('--dest', default=config.ORGANIZED)
    o.add_argument('--apply', action='store_true', help='actually create links (default: preview)')

    a = ap.parse_args(argv)
    if a.cmd == 'serve':
        from .server import serve
        serve(a.db, a.port, a.files, a.import_from, host=a.host, title=a.title or config.TITLE)
    elif a.cmd == 'service':
        from . import macos
        if a.action == 'install':
            return macos.install(a.port, a.lan, a.title)
        return macos.status() if a.action == 'status' else macos.uninstall()
    elif a.cmd == 'user':
        return users_cli(a.action, a.name, a.db)
    elif a.cmd == 'wipe':
        from .wipe import wipe
        return wipe(a.db, a.files, yes=a.yes)
    elif a.cmd == 'check':
        from .doctor import main as check
        return check('ingest' if a.ingest else 'server')
    elif a.cmd == 'demo':
        from .demo import demo
        demo(a.port, a.host)
    elif a.cmd == 'ingest':
        from .ingest import ingest
        ingest(a.source, a.pi, dry_run=a.dry_run)
    elif a.cmd == 'organize':
        from .organize import organize
        organize(a.source, a.dest, apply=a.apply)



def users_cli(action, name, db):
    import getpass
    from .auth import Auth
    from .store import Store, ValidationError
    if not os.path.exists(db):
        raise SystemExit(f'no database at {db}')
    store = Store(db)
    auth = Auth(store)
    try:
        if action == 'list':
            for u in auth.users():
                print(u['username'], u['created'] or '')
            return 0
        if not name:
            raise SystemExit('give a user name')
        if action == 'remove':
            auth.remove_user(name)
        else:
            pw = getpass.getpass(f'New password for {name}: ')
            if pw != getpass.getpass('Again: '):
                raise SystemExit("passwords don't match")
            (auth.add_user if action == 'add' else auth.set_password)(name, pw)
        print('done')
        return 0
    except ValidationError as e:
        raise SystemExit(str(e))
    finally:
        store.close()


if __name__ == '__main__':
    sys.exit(main())
