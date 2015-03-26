from hashlib import md5
import os
from CodernityDB.database import Database, DatabasePathException,  \
                                RecordNotFound
from CodernityDB.hash_index import HashIndex
from devp2p.service import BaseService
from gevent.event import Event
from pyethereum import compress
from pyethereum.slogging import get_logger


log = get_logger('db')


class MD5Index(HashIndex):

    def __init__(self, *args, **kwargs):
        kwargs['key_format'] = '16s'
        super(MD5Index, self).__init__(*args, **kwargs)

    def make_key_value(self, data):
        return md5(data['key']).digest(), None

    def make_key(self, key):
        return md5(key).digest()


class CodernityDB(BaseService):
    """A service providing a codernity db interface."""

    name = 'db'

    def start(self):
        super(CodernityDB, self).start()
        self.dbfile = os.path.join(self.app.config['app']['dir'],
                                   self.app.config['db']['path'])
        self.uncommitted = dict()
        self.db = Database(self.dbfile)
        try:
            log.info('opening db', path=self.dbfile)
            self.db.open()
        except DatabasePathException:
            log.info('db does not exist, creating it', path=self.dbfile)
            self.db.create()
            self.db.add_index(MD5Index(self.dbfile, 'key'))
        self.stop_event = Event()

    def _run(self):
        self.stop_event.wait()

    def stop(self):
        # commit?
        log.info('closing db')
        if self.started:
            self.db.close()
            self.stop_event.set()

    def get(self, key):
        log.debug('getting entry', key=key)
        if key in self.uncommitted:
            if self.uncommitted[key] is None:
                raise KeyError("key not in db")
            return self.uncommitted[key]
        try:
            value = self.db.get('key', key, with_doc=True)['doc']['value']
        except RecordNotFound:
            raise KeyError("key not in db")
        return compress.decompress(value)

    def put(self, key, value):
        log.debug('putting entry', key=key, value=value)
        self.uncommitted[key] = value

    def commit(self):
        log.debug('committing', db=self)
        for k, v in self.uncommitted.items():
            if v is None:
                doc = self.db.get('key', k, with_doc=True)['doc']
                self.db.delete(doc)
            else:
                self.db.insert({'key': k, 'value': compress.compress(v)})
        self.uncommitted.clear()

    def delete(self, key):
        log.debug('deleting entry', key=key)
        self.uncommitted[key] = None

    def __contains__(self, key):
        try:
            self.get(key)
        except KeyError:
            return False
        return True

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.db == other.db

    def __repr__(self):
        return '<DB at %d uncommitted=%d>' % (id(self.db), len(self.uncommitted))
