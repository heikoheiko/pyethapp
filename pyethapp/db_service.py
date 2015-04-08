from devp2p.service import BaseService
from ethereum.slogging import get_logger
log = get_logger('db')


# load available databases
dbs = {}
try:
    from leveldb_service import LevelDB
except ImportError:
    pass
else:
    dbs['LevelDB'] = LevelDB

try:
    from codernitydb_service import CodernityDB
except ImportError:
    pass
else:
    dbs['CodernityDB'] = CodernityDB


class DBService(BaseService):

    name = 'db'
    default_config = dict(db=dict(implementation='LevelDB'))

    def __init__(self, app):
        super(DBService, self).__init__(app)
        impl = self.app.config['db']['implementation']
        if len(dbs) == 0:
            log.warning('No db installed')
        self.db_service = dbs[impl](app)

    def start(self):
        return self.db_service.start()

    def _run(self):
        return self.db_service._run()

    def get(self, key):
        return self.db_service.get(key)

    def put(self, key, value):
        return self.db_service.put(key, value)

    def commit(self):
        return self.db_service.commit()

    def delete(self, key):
        return self.db_service.delete(key)

    def __contains__(self, key):
        return key in self.db_service

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.db_service == other.db_service

    def __repr__(self):
        return repr(self.db_service)
