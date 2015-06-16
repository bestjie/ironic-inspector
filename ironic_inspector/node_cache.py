# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Cache for nodes currently under introspection."""

import contextlib
import json
import logging
import os
import sqlite3
import sys
import time

from ironicclient import exceptions
from oslo_config import cfg

from ironic_inspector.common.i18n import _, _LC, _LE, _LW
from ironic_inspector import utils

CONF = cfg.CONF


LOG = logging.getLogger("ironic_inspector.node_cache")
_DB_NAME = None
_SCHEMA = """
create table if not exists nodes
 (uuid text primary key, started_at real, finished_at real, error text);

create table if not exists attributes
 (name text, value text, uuid text,
  primary key (name, value),
  foreign key (uuid) references nodes);

create table if not exists options
 (uuid text, name text, value text,
  primary key (uuid, name),
  foreign key (uuid) references nodes);
"""


MACS_ATTRIBUTE = 'mac'


class NodeInfo(object):
    """Record about a node in the cache."""

    def __init__(self, uuid, started_at, finished_at=None, error=None,
                 node=None, ports=None):
        self.uuid = uuid
        self.started_at = started_at
        self.finished_at = finished_at
        self.error = error
        self.invalidate_cache()
        self._node = node
        if ports is not None and not isinstance(ports, dict):
            ports = {p.address: p for p in ports}
        self._ports = ports

    @property
    def options(self):
        """Node introspection options as a dict."""
        if self._options is None:
            rows = _db().execute('select name, value from options '
                                 'where uuid=?', (self.uuid,))
            self._options = {row['name']: json.loads(row['value'])
                             for row in rows}
        return self._options

    def set_option(self, name, value):
        """Set an option for a node."""
        encoded = json.dumps(value)
        self.options[name] = value
        with _db() as db:
            db.execute('delete from options where uuid=? and name=?',
                       (self.uuid, name))
            db.execute('insert into options(uuid, name, value) values(?,?,?)',
                       (self.uuid, name, encoded))

    def finished(self, error=None):
        """Record status for this node.

        Also deletes look up attributes from the cache.

        :param error: error message
        """
        self.finished_at = time.time()
        self.error = error

        with _db() as db:
            db.execute('update nodes set finished_at=?, error=? where uuid=?',
                       (self.finished_at, error, self.uuid))
            db.execute("delete from attributes where uuid=?", (self.uuid,))
            db.execute("delete from options where uuid=?", (self.uuid,))

    def add_attribute(self, name, value, database=None):
        """Store look up attribute for a node in the database.

        :param name: attribute name
        :param value: attribute value or list of possible values
        :param database: optional existing database connection
        :raises: Error if attributes values are already in database
        """
        if not isinstance(value, list):
            value = [value]

        with _maybe_db(database) as db:
            try:
                db.executemany("insert into attributes(name, value, uuid) "
                               "values(?, ?, ?)",
                               [(name, v, self.uuid) for v in value])
            except sqlite3.IntegrityError as exc:
                LOG.error(_LE('Database integrity error %s during '
                              'adding attributes'), exc)
                raise utils.Error(_(
                    'Some or all of %(name)s\'s %(value)s are already '
                    'on introspection') % {'name': name, 'value': value})

    @classmethod
    def from_row(cls, row):
        """Construct NodeInfo from a database row."""
        fields = {key: row[key]
                  for key in ('uuid', 'started_at', 'finished_at', 'error')}
        return cls(**fields)

    def invalidate_cache(self):
        """Clear all cached info, so that it's reloaded next time."""
        self._options = None
        self._node = None
        self._ports = None

    def node(self, ironic=None):
        """Get Ironic node object associated with the cached node record."""
        if self._node is None:
            ironic = utils.get_client() if ironic is None else ironic
            self._node = ironic.node.get(self.uuid)
        return self._node

    def create_ports(self, macs, ironic=None):
        """Create one or several ports for this node.

        A warning is issued if port already exists on a node.
        """
        ironic = utils.get_client() if ironic is None else ironic
        for mac in macs:
            if mac not in self.ports():
                self._create_port(mac, ironic)
            else:
                LOG.warn(_LW('Port %(mac)s already exists for node %(uuid)s, '
                             'skipping'), {'mac': mac, 'uuid': self.uuid})

    def ports(self, ironic=None):
        """Get Ironic port objects associated with the cached node record.

        This value is cached as well, use invalidate_cache() to clean.

        :return: dict MAC -> port object
        """
        if self._ports is None:
            ironic = utils.get_client() if ironic is None else ironic
            self._ports = {p.address: p
                           for p in ironic.node.list_ports(self.uuid, limit=0)}
        return self._ports

    def _create_port(self, mac, ironic):
        try:
            port = ironic.port.create(node_uuid=self.uuid, address=mac)
        except exceptions.Conflict:
            LOG.warn(_LW('Port %(mac)s already exists for node %(uuid)s, '
                         'skipping'), {'mac': mac, 'uuid': self.uuid})
            # NOTE(dtantsur): we didn't get port object back, so we have to
            # reload ports on next access
            self._ports = None
        else:
            self._ports[mac] = port


def init():
    """Initialize the database."""
    global _DB_NAME

    _DB_NAME = CONF.database.strip()
    if not _DB_NAME:
        LOG.critical(_LC('Configuration option inspector.database'
                         ' should be set'))
        sys.exit(1)

    db_dir = os.path.dirname(_DB_NAME)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)
    sqlite3.connect(_DB_NAME).executescript(_SCHEMA)


def _db():
    if _DB_NAME is None:
        init()
    conn = sqlite3.connect(_DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def _maybe_db(db=None):
    if db is None:
        with _db() as db:
            yield db
    else:
        yield db


def add_node(uuid, **attributes):
    """Store information about a node under introspection.

    All existing information about this node is dropped.
    Empty values are skipped.

    :param uuid: Ironic node UUID
    :param attributes: attributes known about this node (like macs, BMC etc)
    :returns: NodeInfo
    """
    started_at = time.time()
    with _db() as db:
        db.execute("delete from nodes where uuid=?", (uuid,))
        db.execute("delete from attributes where uuid=?", (uuid,))
        db.execute("delete from options where uuid=?", (uuid,))

        db.execute("insert into nodes(uuid, started_at) "
                   "values(?, ?)", (uuid, started_at))

        node_info = NodeInfo(uuid=uuid, started_at=started_at)
        for (name, value) in attributes.items():
            if not value:
                continue
            node_info.add_attribute(name, value, database=db)

    return node_info


def active_macs():
    """List all MAC's that are on introspection right now."""
    return {x[0] for x in _db().execute("select value from attributes "
                                        "where name=?", (MACS_ATTRIBUTE,))}


def get_node(uuid):
    """Get node from cache by it's UUID.

    :param uuid: node UUID.
    :returns: structure NodeInfo.
    """
    row = _db().execute('select * from nodes where uuid=?', (uuid,)).fetchone()
    if row is None:
        raise utils.Error(_('Could not find node %s in cache') % uuid,
                          code=404)
    return NodeInfo.from_row(row)


def find_node(**attributes):
    """Find node in cache.

    :param attributes: attributes known about this node (like macs, BMC etc)
    :returns: structure NodeInfo with attributes ``uuid`` and ``created_at``
    :raises: Error if node is not found
    """
    # NOTE(dtantsur): sorting is not required, but gives us predictability
    found = set()
    db = _db()
    for (name, value) in sorted(attributes.items()):
        if not value:
            LOG.debug('Empty value for attribute %s', name)
            continue
        if not isinstance(value, list):
            value = [value]

        LOG.debug('Trying to use %s of value %s for node look up'
                  % (name, value))
        rows = db.execute('select distinct uuid from attributes where ' +
                          ' OR '.join('name=? AND value=?' for _ in value),
                          sum(([name, v] for v in value), [])).fetchall()
        if rows:
            found.update(item[0] for item in rows)

    if not found:
        raise utils.NotFoundInCacheError(_(
            'Could not find a node for attributes %s') % attributes)
    elif len(found) > 1:
        raise utils.Error(_(
            'Multiple matching nodes found for attributes %(attr)s: %(found)s')
            % {'attr': attributes, 'found': list(found)}, code=404)

    uuid = found.pop()
    row = db.execute('select started_at, finished_at from nodes where uuid=?',
                     (uuid,)).fetchone()
    if not row:
        raise utils.Error(_(
            'Could not find node %s in introspection cache, '
            'probably it\'s not on introspection now') % uuid, code=404)

    if row['finished_at']:
        raise utils.Error(_(
            'Introspection for node %(node)s already finished on %(finish)s') %
            {'node': uuid, 'finish': row['finished_at']})

    return NodeInfo(uuid=uuid, started_at=row['started_at'])


def clean_up():
    """Clean up the cache.

    * Finish introspection for timed out nodes.
    * Drop outdated node status information.

    :return: list of timed out node UUID's
    """
    status_keep_threshold = (time.time() -
                             CONF.node_status_keep_time)

    with _db() as db:
        db.execute('delete from nodes where finished_at < ?',
                   (status_keep_threshold,))

    timeout = CONF.timeout
    if timeout <= 0:
        return []

    threshold = time.time() - timeout
    with _db() as db:
        uuids = [row[0] for row in
                 db.execute('select uuid from nodes where '
                            'started_at < ? and finished_at is null',
                            (threshold,))]
        if not uuids:
            return []

        LOG.error(_LE('Introspection for nodes %s has timed out'), uuids)
        db.execute('update nodes set finished_at=?, error=? '
                   'where started_at < ? and finished_at is null',
                   (time.time(), 'Introspection timeout', threshold))
        db.executemany('delete from attributes where uuid=?',
                       [(u,) for u in uuids])
        db.executemany('delete from options where uuid=?',
                       [(u,) for u in uuids])

    return uuids
