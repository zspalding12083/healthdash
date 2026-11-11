"""Run the dashboard as a macOS launchd agent: starts at login, restarts if it stops.

  python3 -m healthdash service install [--port 9380] [--lan] [--title "Our Health"]
  python3 -m healthdash service status
  python3 -m healthdash service uninstall          (keeps your data)
"""

import os
import plistlib
import subprocess
import sys
import time
import urllib.request

from . import config

LABEL = 'local.healthdash'
HOME = os.path.expanduser('~')
DATA = os.path.join(HOME, 'Library', 'Application Support', 'healthdash')
LOG = os.path.join(HOME, 'Library', 'Logs', 'healthdash.log')
PLIST = os.path.join(HOME, 'Library', 'LaunchAgents', LABEL + '.plist')
# launchd starts agents with a bare PATH; Homebrew's poppler/tesseract live in these
PATH = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'


def agent(port=9380, lan=False, title=None, python=None, project=None, data=DATA, log=LOG):
    """The launchd job description (a dict, written as a plist)."""
    project = project or config.PROJECT
    return {
        'Label': LABEL,
        'ProgramArguments': [python or sys.executable, '-m', 'healthdash', 'serve',
                             '--db', os.path.join(data, 'health.db'), '--files', os.path.join(data, 'files'),
                             '--port', str(port), '--host', '0.0.0.0' if lan else '127.0.0.1',
                             '--title', title or config.TITLE],
        'WorkingDirectory': project,
        'EnvironmentVariables': {'PYTHONPATH': project, 'PATH': PATH},
        'RunAtLoad': True,
        'KeepAlive': True,
        'Umask': 0o077,
        'StandardOutPath': log,
        'StandardErrorPath': log,
        'ProcessType': 'Background',
    }


def _launchctl(*args):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True)


def _domain():
    return f'gui/{os.getuid()}'


def _up(port, wait=10):
    for _ in range(wait * 2):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/auth', timeout=2) as r:
                return r.status == 200
        except OSError:
            time.sleep(0.5)
    return False


def _need_mac():
    if sys.platform != 'darwin':
        raise SystemExit('`service` is for macOS. On Linux use deploy/install.sh (systemd).')


def install(port=9380, lan=False, title=None):
    _need_mac()
    os.makedirs(os.path.join(DATA, 'files'), mode=0o700, exist_ok=True)
    os.chmod(DATA, 0o700)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, 'wb') as f:
        plistlib.dump(agent(port, lan, title), f)
    _launchctl('bootout', _domain(), PLIST)  # restart if it was already running
    r = _launchctl('bootstrap', _domain(), PLIST)
    if r.returncode != 0:
        raise SystemExit(f'launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}')
    if _up(port):
        print(f'healthdash is running: http://127.0.0.1:{port}' + (' (also open to your network)' if lan else ''))
        print('Open it and create the first account.')
    else:
        print(f'Started, but it did not answer on port {port}. See the log: {LOG}')
    from .doctor import main as check
    print('\nChecking for the tools that read PDFs and scans:')
    check('server')
    return 0


def uninstall():
    _need_mac()
    _launchctl('bootout', _domain(), PLIST)
    if os.path.exists(PLIST):
        os.remove(PLIST)
    print(f'Stopped and removed. Your data is still in {DATA}')
    return 0


def status():
    _need_mac()
    if not os.path.exists(PLIST):
        print('Not installed. Run: python3 -m healthdash service install')
        return 1
    with open(PLIST, 'rb') as f:
        job = plistlib.load(f)
    args = job['ProgramArguments']
    port = int(args[args.index('--port') + 1])
    running = _launchctl('print', f'{_domain()}/{LABEL}').returncode == 0
    up = running and _up(port, wait=2)
    print(f"{'running' if up else 'NOT answering'} on http://127.0.0.1:{port}  "
          f"(data: {DATA}, log: {LOG})")
    return 0 if up else 1
