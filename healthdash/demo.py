"""Run the dashboard on made-up sample data (sample/), in a throwaway database."""

import json
import os
import tempfile

from . import config
from .server import serve
from .store import KINDS, Store

SAMPLE = os.path.join(config.PROJECT, 'sample')


def load_sample(store):
    with open(os.path.join(SAMPLE, 'documents.json'), encoding='utf-8') as f:
        docs = json.load(f)
    for d in docs:
        d.setdefault('needs_review', False)
    store.upsert_documents(docs)
    with open(os.path.join(SAMPLE, 'records.json'), encoding='utf-8') as f:
        records = json.load(f)
    for kind, items in records.items():
        if kind in KINDS:
            store.upsert_imported(kind, items)


def demo(port, host='127.0.0.1'):
    tmp = tempfile.mkdtemp(prefix='healthdash-demo-')
    db = os.path.join(tmp, 'demo.db')
    store = Store(db)
    load_sample(store)
    store.close()
    serve(db, port, os.path.join(tmp, 'files'), ['127.0.0.1'], host=host,
          title='Health Records (demo)')
