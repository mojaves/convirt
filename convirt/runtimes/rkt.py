#
# Copyright 2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published
# by the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
from __future__ import absolute_import

import json
import logging
import os
import os.path

from ..config import network
from .. import command
from .. import fs
from .. import runner
from . import ContainerRuntime, NotYetReady


command.executables['rkt'] = command.Path('rkt')


_TEMPLATES = {
    'rkt_run':
        '--uuid-file-save=${uuid_path} '
        '--insecure-options=image '  # FIXME
        '--net=${network} '
        'run '
        '${image} '
        '--memory=${memsize}M',

    'rkt_status':
        'status '
        '${rkt_uuid}',
}


def register():
    res = {}
    if Rkt.available():
        res = {
            Rkt.NAME: Rkt
        }
    return res


class Rkt(ContainerRuntime):

    NAME = 'rkt'

    _log = logging.getLogger('convirt.runtime.Rkt')

    _PREFIX = 'rkt-'

    _RKT_UUID_FILE = 'rkt_uuid'

    @staticmethod
    def available():
        rkt = command.executables['rkt']
        return rkt.available

    def __init__(self, conf, repo,
                 rt_uuid=None, read_file=fs.read_file):
        super(Rkt, self).__init__(conf, repo, rt_uuid)
        self._read_file = read_file
        rkt_uuid_file = '%s.%s' % (self._uuid, self.NAME)
        self._rkt_uuid_path = os.path.join(
            self._conf.run_dir, rkt_uuid_file
        )
        self._log.debug(
            'rkt runtime %s uuid_path=[%s]',
            self._uuid, self._rkt_uuid_path
        )
        self._rkt_uuid = None
        self._rkt_run = self._runner.repo.get(
            'rkt', _TEMPLATES['rkt_run'],
            uuid_path=self._rkt_uuid_path,
        )
        self._rkt_status = self._runner.repo.get(
            'rkt', _TEMPLATES['rkt_status'],
        )

    @classmethod
    def configure_runtime(cls):
        conf = network.current()
        with Network(conf.name) as net:
            net.update(conf)

    @property
    def running(self):
        return self._rkt_uuid is not None

    def start(self, target=None):
        if self.running:
            raise runner.OperationFailed('already running')
        fs.rm_file(self._rkt_uuid_path)
        self._runner.start(
            command=self._rkt_run,
            image=self._run_conf.image_path if target is None else target,
            network=self._run_conf.network,
            memsize=self._run_conf.memory_size_mib,
        )
        self._retry(
            'read rkt UUID', self._read_rkt_uuid, self._rkt_uuid_path
        )
        self._retry(
            'fetch rkt state', self._fetch_rkt_state
        )

    def stop(self):
        if not self.running:
            raise runner.OperationFailed('not running')

        self._runner.stop(self.runtime_name())
        try:
            os.remove(self._rkt_uuid_path)
        except OSError:
            pass  # TODO
        self._rkt_uuid = None

    def runtime_name(self):
        if self._rkt_uuid is None:
            return None
        return '%s%s' % (self._PREFIX, self._rkt_uuid)

    def _read_rkt_uuid(self, path):
        try:
            data = self._read_file(path)
        except IOError:
            raise NotYetReady('container not running')
        else:
            self._rkt_uuid = data.strip()
            self._log.info('rkt container %s rkt_uuid %s',
                           self._uuid, self._rkt_uuid)

    def _fetch_rkt_state(self):
        out = self._rkt_status(rkt_uuid=self._rkt_uuid)

        # TODO: find a better solution
        data = _parse_keyval(out.decode('utf-8'))
        if data['state'] != 'running':
            raise NotYetReady('container not running')

        # the pid is still stored, so it is meaningful only if running
        self._pid = int(data['pid'])
        self._log.info('rkt container %s rkt_uuid %s state %s',
                       self._uuid, self._rkt_uuid, data['state'])


def _parse_keyval(output):
    res = {}
    for line in output.splitlines():
        if not line:
            continue
        key, val = line.split('=', 1)
        res[key] = val
    return res


class Network(object):

    DIR = '/etc/rkt/net.d'

    _NAME_TMPL = '50-%s.conf'

    _log = logging.getLogger('convirt.runtime.Rkt')

    def __init__(self, name):
        self._name = name
        self._path = self._NAME_TMPL % name
        self._data = {}
        self._dirty = False

    @property
    def filename(self):
        return self._path

    def __enter__(self):
        self.load()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.save()

    def __eq__(self, other):
        return self._data == other._data

    @property
    def path(self):
        return os.path.join(self.DIR, self._path)

    def update(self, conf):
        new_data = self._make(conf)
        if self._data != new_data:
            self._data = new_data
            self._dirty = True

    def load(self):
        try:
            with open(self.path, 'rt') as src:
                self._data = json.load(src)
        except IOError:
            self._log.debug('config: cannot load %r, ignored', self.path)
        return self._data

    def save(self, force=False):
        if not self._dirty and not force:
            self._log.info('config: no update needed, save skipped')
        else:
            with open(self.path, 'wt') as dst:
                json.dump(self._data, dst, indent=2, sort_keys=True)

    def clear(self):
        fs.rm_file(self.path)

    # test/debug purposes
    def get_conf(self):
        net, mask = self._data["ipam"]["subnet"].split('/')
        return {
            "name": self._data["name"],
            "bridge": self._data["bridge"],
            "subnet": net,
            "mask": int(mask),
        }

    def _make(self, conf):
        bridge = conf["bridge"]
        self._log.debug('config: using bridge %r for %r',
                        bridge, self._name)
        return {
            "name": self._name,
            "type": "bridge",
            "bridge": bridge,
            "ipam": self._make_ipam(conf)
        }

    def _make_ipam(self, conf):
        subnet = "%s/%s" % (conf["subnet"], conf["mask"])
        self._log.debug('config: using subnet %r', subnet)
        return {
            "type": "host-local",
            "subnet": subnet,
        }
