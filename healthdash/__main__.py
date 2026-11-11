"""healthdash command line.

  python3 -m healthdash demo    [--port 9380]                          (made-up sample data)
  python3 -m healthdash serve   --db DB --port 9380 --files DIR --import-from INGEST_HOST_IP --title "Our Health"
  python3 -m healthdash ingest  [--source DIR] [--pi URL] [--dry-run]      (run where the files are)
  python3 -m healthdash organize [--source DIR] [--dest DIR] [--apply]     (run where the files are)
"""

import argparse
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
        serve(a.db, a.port, a.files, a.import_from, title=a.title or config.TITLE)
    elif a.cmd == 'demo':
        from .demo import demo
        demo(a.port, a.host)
    elif a.cmd == 'ingest':
        from .ingest import ingest
        ingest(a.source, a.pi, dry_run=a.dry_run)
    elif a.cmd == 'organize':
        from .organize import organize
        organize(a.source, a.dest, apply=a.apply)


if __name__ == '__main__':
    sys.exit(main())
