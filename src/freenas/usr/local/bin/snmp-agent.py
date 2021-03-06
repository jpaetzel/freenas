#!/usr/local/bin/python

from collections import defaultdict
import copy
from datetime import datetime, timedelta
import os
import subprocess
import sys
import threading

import libzfs
import netsnmpagent
import pysnmp.hlapi  # noqa
import pysnmp.smi

sys.path.append("/usr/local/www")
from freenasUI.tools.arc_summary import get_Kstat, get_arc_efficiency


def calculate_allocation_units(*args):
    allocation_units = 4096
    while True:
        values = tuple(map(lambda arg: int(arg / allocation_units), args))
        if all(v < 2 ** 31 for v in values):
            break

        allocation_units *= 2

    return allocation_units, values


def get_zfs_arc_miss_percent(kstat):
    arc_hits = kstat["kstat.zfs.misc.arcstats.hits"]
    arc_misses = kstat["kstat.zfs.misc.arcstats.misses"]
    arc_read = arc_hits + arc_misses
    if arc_read > 0:
        hit_percent = float(100 * arc_hits / arc_read)
        miss_percent = 100 - hit_percent
        return miss_percent
    return 0


mib_builder = pysnmp.smi.builder.MibBuilder()
mib_sources = mib_builder.getMibSources() + (pysnmp.smi.builder.DirMibSource("/usr/local/share/pysnmp/mibs"),)
mib_builder.setMibSources(*mib_sources)
mib_builder.loadModules("FREENAS-MIB")
zpool_health_type = mib_builder.importSymbols("FREENAS-MIB", "ZPoolHealthType")[0]

agent = netsnmpagent.netsnmpAgent(
    AgentName="FreeNASAgent",
    MIBFiles=["/usr/local/share/snmp/mibs/FREENAS-MIB.txt"],
)

zpool_table = agent.Table(
    oidstr="FREENAS-MIB::zpoolTable",
    indexes=[
        agent.Integer32()
    ],
    columns=[
        (2, agent.DisplayString()),
        (3, agent.Integer32()),
        (4, agent.Integer32()),
        (5, agent.Integer32()),
        (6, agent.Integer32()),
        (7, agent.Integer32()),
        (8, agent.Counter64()),
        (9, agent.Counter64()),
        (10, agent.Counter64()),
        (11, agent.Counter64()),
        (12, agent.Counter64()),
        (13, agent.Counter64()),
        (14, agent.Counter64()),
        (15, agent.Counter64()),
    ],
)

dataset_table = agent.Table(
    oidstr="FREENAS-MIB::datasetTable",
    indexes=[
        agent.Integer32()
    ],
    columns=[
        (2, agent.DisplayString()),
        (3, agent.Integer32()),
        (4, agent.Integer32()),
        (5, agent.Integer32()),
        (6, agent.Integer32()),
    ],
)

zvol_table = agent.Table(
    oidstr="FREENAS-MIB::zvolTable",
    indexes=[
        agent.Integer32()
    ],
    columns=[
        (2, agent.DisplayString()),
        (3, agent.Integer32()),
        (4, agent.Integer32()),
        (5, agent.Integer32()),
        (6, agent.Integer32()),
    ],
)

zfs_arc_size = agent.Unsigned32(oidstr="FREENAS-MIB::zfsArcSize")
zfs_arc_meta = agent.Unsigned32(oidstr="FREENAS-MIB::zfsArcMeta")
zfs_arc_data = agent.Unsigned32(oidstr="FREENAS-MIB::zfsArcData")
zfs_arc_hits = agent.Unsigned32(oidstr="FREENAS-MIB::zfsArcHits")
zfs_arc_misses = agent.Unsigned32(oidstr="FREENAS-MIB::zfsArcMisses")
zfs_arc_c = agent.Unsigned32(oidstr="FREENAS-MIB::zfsArcC")
zfs_arc_p = agent.Unsigned32(oidstr="FREENAS-MIB::zfsArcP")
zfs_arc_miss_percent = agent.DisplayString(oidstr="FREENAS-MIB::zfsArcMissPercent")
zfs_arc_cache_hit_ratio = agent.DisplayString(oidstr="FREENAS-MIB::zfsArcCacheHitRatio")
zfs_arc_cache_miss_ratio = agent.DisplayString(oidstr="FREENAS-MIB::zfsArcCacheMissRatio")

zfs_l2arc_hits = agent.Counter32(oidstr="FREENAS-MIB::zfsL2ArcHits")
zfs_l2arc_misses = agent.Counter32(oidstr="FREENAS-MIB::zfsL2ArcMisses")
zfs_l2arc_read = agent.Counter32(oidstr="FREENAS-MIB::zfsL2ArcRead")
zfs_l2arc_write = agent.Counter32(oidstr="FREENAS-MIB::zfsL2ArcWrite")
zfs_l2arc_size = agent.Unsigned32(oidstr="FREENAS-MIB::zfsL2ArcSize")

zfs_zilstat_ops1 = agent.Counter64(oidstr="FREENAS-MIB::zfsZilstatOps1sec")
zfs_zilstat_ops5 = agent.Counter64(oidstr="FREENAS-MIB::zfsZilstatOps5sec")
zfs_zilstat_ops10 = agent.Counter64(oidstr="FREENAS-MIB::zfsZilstatOps10sec")


class ZpoolIoThread(threading.Thread):
    def __init__(self):
        super().__init__()

        self.daemon = True

        self.stop_event = threading.Event()

        self.lock = threading.Lock()
        self.values_overall = defaultdict(lambda: defaultdict(lambda: 0))
        self.values_1s = defaultdict(lambda: defaultdict(lambda: 0))

    def run(self):
        zfs = libzfs.ZFS()
        while not self.stop_event.wait(1.0):
            with self.lock:
                previous_values = copy.deepcopy(self.values_overall)

                for pool in zfs.pools:
                    self.values_overall[pool.name] = {
                        "read_ops": pool.root_vdev.stats.ops[libzfs.ZIOType.READ],
                        "write_ops": pool.root_vdev.stats.ops[libzfs.ZIOType.WRITE],
                        "read_bytes": pool.root_vdev.stats.bytes[libzfs.ZIOType.READ],
                        "write_bytes": pool.root_vdev.stats.bytes[libzfs.ZIOType.WRITE],
                    }

                    if pool.name in previous_values:
                        for k in ["read_ops", "write_ops", "read_bytes", "write_bytes"]:
                            self.values_1s[pool.name][k] = (
                                self.values_overall[pool.name][k] -
                                previous_values[pool.name][k]
                            )

    def get_values(self):
        with self.lock:
            return copy.deepcopy(self.values_overall), copy.deepcopy(self.values_1s)


class ZilstatThread(threading.Thread):
    def __init__(self, interval):
        super().__init__()

        self.daemon = True

        self.interval = interval
        self.value = {
            "NBytes": 0,
            "NBytespersec": 0,
            "NMaxRate": 0,
            "BBytes": 0,
            "BBytespersec": 0,
            "BMaxRate": 0,
            "ops": 0,
            "lteq4kb": 0,
            "4to32kb": 0,
            "gteq4kb": 0,
        }

    def run(self):
        zilstatproc = subprocess.Popen(
            ["/usr/local/bin/zilstat", str(self.interval)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        zilstatproc.stdout.readline().strip()
        while zilstatproc.poll() is None:
            output = zilstatproc.stdout.readline().strip().split()
            value = {
                "NBytes": output[0],
                "NBytespersec": output[1],
                "NMaxRate": output[2],
                "BBytes": output[3],
                "BBytespersec": output[4],
                "BMaxRate": output[5],
                "ops": int(output[6]),
                "lteq4kb": output[7],
                "4to32kb": output[8],
                "gteq4kb": output[9],
            }
            self.value = value


if __name__ == "__main__":
    zfs = libzfs.ZFS()

    zpool_io_thread = ZpoolIoThread()
    zpool_io_thread.start()

    zilstat_1_thread = ZilstatThread(1)
    zilstat_1_thread.start()

    zilstat_5_thread = ZilstatThread(5)
    zilstat_5_thread.start()

    zilstat_10_thread = ZilstatThread(10)
    zilstat_10_thread.start()

    agent.start()

    last_update_at = datetime.min
    while True:
        agent.check_and_process()

        if datetime.utcnow() - last_update_at > timedelta(seconds=1):
            zpool_io_overall, zpool_io_1sec = zpool_io_thread.get_values()

            datasets = []
            zvols = []
            zpool_table.clear()
            for i, zpool in enumerate(zfs.pools):
                row = zpool_table.addRow([agent.Integer32(i)])
                row.setRowCell(2, agent.DisplayString(zpool.properties["name"].value))
                allocation_units, \
                    (
                        size,
                        used,
                        available
                    ) = calculate_allocation_units(
                        int(zpool.properties["size"].rawvalue),
                        int(zpool.properties["allocated"].rawvalue),
                        int(zpool.properties["free"].rawvalue),
                    )
                row.setRowCell(3, agent.Integer32(allocation_units))
                row.setRowCell(4, agent.Integer32(size))
                row.setRowCell(5, agent.Integer32(used))
                row.setRowCell(6, agent.Integer32(available))
                row.setRowCell(7, agent.Integer32(zpool_health_type.namedValues.getValue(
                    zpool.properties["health"].value.lower())))
                row.setRowCell(8, agent.Counter64(zpool_io_overall[zpool.name]["read_ops"]))
                row.setRowCell(9, agent.Counter64(zpool_io_overall[zpool.name]["write_ops"]))
                row.setRowCell(10, agent.Counter64(zpool_io_overall[zpool.name]["read_bytes"]))
                row.setRowCell(11, agent.Counter64(zpool_io_overall[zpool.name]["write_bytes"]))
                row.setRowCell(12, agent.Counter64(zpool_io_1sec[zpool.name]["read_ops"]))
                row.setRowCell(13, agent.Counter64(zpool_io_1sec[zpool.name]["write_ops"]))
                row.setRowCell(14, agent.Counter64(zpool_io_1sec[zpool.name]["read_bytes"]))
                row.setRowCell(15, agent.Counter64(zpool_io_1sec[zpool.name]["write_bytes"]))

                for dataset in zpool.root_dataset.children_recursive:
                    if dataset.type == libzfs.DatasetType.FILESYSTEM:
                        datasets.append(dataset)
                    if dataset.type == libzfs.DatasetType.VOLUME:
                        zvols.append(dataset)

            dataset_table.clear()
            for i, dataset in enumerate(datasets):
                row = dataset_table.addRow([agent.Integer32(i)])
                row.setRowCell(2, agent.DisplayString(dataset.properties["name"].value))
                allocation_units, (
                    size,
                    used,
                    available
                ) = calculate_allocation_units(
                    int(dataset.properties["used"].rawvalue) + int(dataset.properties["available"].rawvalue),
                    int(dataset.properties["used"].rawvalue),
                    int(dataset.properties["available"].rawvalue),
                )
                row.setRowCell(3, agent.Integer32(allocation_units))
                row.setRowCell(4, agent.Integer32(size))
                row.setRowCell(5, agent.Integer32(used))
                row.setRowCell(6, agent.Integer32(available))

            zvol_table.clear()
            for i, zvol in enumerate(zvols):
                row = zvol_table.addRow([agent.Integer32(i)])
                row.setRowCell(2, agent.DisplayString(zvol.properties["name"].value))
                allocation_units, (
                    volsize,
                    used,
                    available
                ) = calculate_allocation_units(
                    int(zvol.properties["volsize"].rawvalue),
                    int(zvol.properties["used"].rawvalue),
                    int(zvol.properties["available"].rawvalue),
                )
                row.setRowCell(3, agent.Integer32(allocation_units))
                row.setRowCell(4, agent.Integer32(volsize))
                row.setRowCell(5, agent.Integer32(used))
                row.setRowCell(6, agent.Integer32(available))

            last_update_at = datetime.utcnow()

            kstat = get_Kstat()
            arc_efficiency = get_arc_efficiency(kstat)

            zfs_arc_size.update(kstat["kstat.zfs.misc.arcstats.size"] / 1024)
            zfs_arc_meta.update(kstat["kstat.zfs.misc.arcstats.arc_meta_used"] / 1024)
            zfs_arc_data.update(kstat["kstat.zfs.misc.arcstats.data_size"] / 1024)
            zfs_arc_hits.update(kstat["kstat.zfs.misc.arcstats.hits"] % 2 ** 32)
            zfs_arc_misses.update(kstat["kstat.zfs.misc.arcstats.misses"] % 2 ** 32)
            zfs_arc_c.update(kstat["kstat.zfs.misc.arcstats.c"] / 1024)
            zfs_arc_p.update(kstat["kstat.zfs.misc.arcstats.p"] / 1024)
            zfs_arc_miss_percent.update(str(get_zfs_arc_miss_percent(kstat)).encode("ascii"))
            zfs_arc_cache_hit_ratio.update(str(arc_efficiency["cache_hit_ratio"]["per"][:-1]).encode("ascii"))
            zfs_arc_cache_miss_ratio.update(str(arc_efficiency["cache_miss_ratio"]["per"][:-1]).encode("ascii"))

            zfs_l2arc_hits.update(int(kstat["kstat.zfs.misc.arcstats.l2_hits"] % 2 ** 32))
            zfs_l2arc_misses.update(int(kstat["kstat.zfs.misc.arcstats.l2_misses"] % 2 ** 32))
            zfs_l2arc_read.update(int(kstat["kstat.zfs.misc.arcstats.l2_read_bytes"] / 1024 % 2 ** 32))
            zfs_l2arc_write.update(int(kstat["kstat.zfs.misc.arcstats.l2_write_bytes"] / 1024 % 2 ** 32))
            zfs_l2arc_size.update(int(kstat["kstat.zfs.misc.arcstats.l2_size"] / 1024))

            zfs_zilstat_ops1.update(zilstat_1_thread.value["ops"])
            zfs_zilstat_ops5.update(zilstat_5_thread.value["ops"])
            zfs_zilstat_ops10.update(zilstat_10_thread.value["ops"])
