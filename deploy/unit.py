"""Write a systemd unit for one dashboard from deploy/healthdash.service.

  python3 deploy/unit.py OUT STATE PORT IMPORT_FROM TITLE

STATE is the service and data-folder name (healthdash, healthdash-alex, ...): the database and
files live in /var/lib/STATE, created by systemd and readable only by the healthdash user.
"""
import os
import re
import sys

out, state, port, imp, title = sys.argv[1:6]
if not re.fullmatch(r'healthdash(-[a-z0-9][a-z0-9-]{0,30})?', state):
    sys.exit(f'bad dashboard name: {state}')
if not port.isdigit() or not 1024 < int(port) < 65536:
    sys.exit(f'bad port: {port}')
here = os.path.dirname(os.path.abspath(__file__))
s = open(os.path.join(here, 'healthdash.service')).read()
title = title.replace('\\', '').replace('"', "'").replace('%', '%%')
for k, v in (('__STATE__', state), ('__PORT__', port), ('__IMPORT_FROM__', imp), ('__TITLE__', title)):
    s = s.replace(k, v)
open(out, 'w').write(s)
