'''SNMPTrap input plugin for Snooze'''

import datetime
import logging
import os
import yaml
from multiprocessing import JoinableQueue
from threading import Thread
from pathlib import Path

from pysnmp.carrier.asyncore.dgram import udp
from pysnmp.entity import engine, config
from pysnmp.entity.rfc3413 import ntfrcv
from pysnmp.entity.rfc3413.mibvar import oidToMibName, cloneFromMibValue
from pysnmp.proto.rfc1902 import *
from pysnmp.smi import view, compiler, builder
from pysnmp.smi.error import MibNotFoundError, NoSuchObjectError

from snooze_client import Snooze

log = logging.getLogger("snooze.snmptrap")
logging.basicConfig(format="%(asctime)s - %(name)s: %(levelname)s - %(message)s", level=logging.DEBUG)

MAP_TABLE = {
}

class SNMPTrap:
    def __init__(
            self,
            queue,
            bind_address='0.0.0.0',
            port=1163,
            mib_dirs=['/usr/share/snmp/mibs'],
            mib_list=[],
    ):
        self.queue = queue

        self.mib_dirs = mib_dirs
        self.mib_list = mib_list

        self.snmp_engine = engine.SnmpEngine()
        config.addTransport(
            self.snmp_engine,
            udp.domainName + (1,),
            udp.UdpTransport().openServerMode((bind_address, port))
        )
        config.addV1System(self.snmp_engine, 'my-area', 'public')
        ntfrcv.NotificationReceiver(self.snmp_engine, self._cbFun)

        self._load_mibs()

    def _load_mibs(self):
        '''Load the MIB files'''
        snmp_builder = builder.MibBuilder()
        snmp_view = view.MibViewController(snmp_builder)
        mib_dirs = ["file:/{}".format(path) for path in self.mib_dirs]
        compiler.addMibCompiler(snmp_builder, sources=mib_dirs)
        snmp_builder.loadModules(*self.mib_list)
        self.view = snmp_view

    # pylint: disable=too-many-arguments, invalid-name
    def _cbFun(self, snmp_engine, state, context_id, context_name, var_binds, cbctx):
        '''Handler required by pysnmp. Following their naming convention'''
        try:
            execContext = snmp_engine.observer.getExecutionContext('rfc3412.receiveMessage:request')
            source_ip, _ = execContext['transportAddress']
            record = self._handler(var_binds)
            record['source_ip'] = source_ip
            record['source'] = 'snmptrap'
            now = datetime.datetime.now().astimezone()
            record['timestamp'] = now.isoformat()
            self.queue.put(record)
        except Exception as err:
            log.warning(err)
            log.warning("Failed to process: %s", var_binds)

    def _handler(self, oids):
        '''Handler called by each incoming SNMP trap'''
        record = {}
        for oid, value in oids:
            key, value = self._process_mib(oid, value)
            if key and value:
                record[key.replace('.', '_')] = value
        return record

    def _process_mib(self, oid, value):
        '''
        Translate the OID and value depending on the MIB.
        Will translate oid to human readable values.
        Will translate the value to its SNMP type.
        '''
        try:
            (symbol, module), indices = oidToMibName(self.view, oid)
            if (module, symbol) == ('SNMPv2-MIB', 'snmpTrapOID'):
                (trap_mod, trap_sym), _ = oidToMibName(self.view, value)
                return "oid", "{}::{}".format(trap_sym, trap_mod)
            else:
                trap_value = cloneFromMibValue(self.view, module, symbol, value)
                name = "{}::{}".format(module, symbol)
                for suffix in indices:
                    name += ".{}".format(suffix)
                return name, str(trap_value)
        except (MibNotFoundError, NoSuchObjectError):
            log.warning("Could not find OID: %s", oid)
            return str(oid), str(value)

    def reload(self):
        pass

    def start(self):
        '''Make the SNMP trap server listen to incoming traps'''
        self.snmp_engine.transportDispatcher.jobStarted(1)
        try:
            self.snmp_engine.transportDispatcher.runDispatcher()
        except Exception as err:
            raise err
        finally:
            self.snmp_engine.transportDispatcher.closeDispatcher()

def snmp_map(record):
    '''Map certain common SNMPTrap OIDs to field names used by Snooze'''
    for key, value in record.items():
        log.debug("Mapping %s, %s", key, value)
        # Mapping SNMP types to JSON serializable types
        value_type = type(value)
        if value_type == Null:
            value = None
        elif value_type in [Integer, Integer32, Unsigned32, Gauge32, Counter64]:
            value = int(value)
        elif value_type in [OctetString, Opaque]:
            value = str(value)
        elif value_type == Bits:
            value = value.pretty_print()
        elif value_type == IpAddress:
            value = str(value)
        elif value_type == ObjectIdentifier:
            value = str(value)
        elif value_type == TimeTicks:
            # Change the timetick to seconds
            value = int(value) / 100
        else:
            value = str(value)

        record[key] = value

        log.debug("New value: %s", value)

        # Mapping SNMP OIDs to fields used by Snooze
        if key in MAP_TABLE:
            new_key = MAP_TABLE[key]
            log.debug("Mapping %s=>%s", key, new_key)
            record[new_key] = value

    return record

class Main:
    def __init__(self):
        # config
        self.config = {}

        config_file = os.environ.get('SNOOZE_SNMPTRAP_CONFIG') or '/etc/snooze/snmptrap.yaml'
        config_file = Path(config_file)
        try:
            with config_file.open('r') as myfile:
                self.config = yaml.safe_load(myfile.read())
        except Exception as err:
            log.error("Error loading config: %s", err)

        if not isinstance(self.config, dict):
            self.config = {}

        snooze_uri = self.config.get('snooze_server', None)
        self.api = Snooze(snooze_uri)

        self.send_workers_pool = self.config.get('send_workers', 4)

        listening_address = self.config.get('listening_address', '0.0.0.0')
        listening_port = self.config.get('listening_port', 1163)
        mib_dirs = self.config.get('mib_dirs', ['/usr/share/snmp/mibs'])

        self.send_queue = JoinableQueue()
        self.snmp_server = SNMPTrap(
            self.send_queue,
            bind_address=listening_address,
            port=listening_port,
            mib_dirs=mib_dirs,
            mib_list=[],
        )
        self.snmp_thread = Thread(target=self.snmp_server.start)

    def start_send_workers(self, worker_pool):
        threads = []
        for index in range(worker_pool):
            mythread = Thread(target=self.send_worker, args=(index,))
            mythread.start()
            threads.append(mythread)
        return threads

    def send_worker(self, index):
        '''A worker for sending records to Snooze'''
        while True:
            log.debug("[send_record] Waiting for queue")
            record = self.send_queue.get()
            if not record:
                log.info("Stopping send worker %d", index)
                break
            snmp_map(record)
            log.debug("Sending record to snooze: %s", record)
            self.api.alert(record)

    def stop_threads(self, queue, threads):
        for _ in threads:
            queue.put(None)
        for thread in threads:
            thread.join()

    def run(self):
        try:
            self.snmp_thread.start()
            send_threads = self.start_send_workers(self.send_workers_pool)

            threads = [self.snmp_thread] + send_threads
            for thread in threads:
                thread.join()
        finally:
            log.info("Stopping SNMP listener")
            transportDispatcher = self.snmp_server.snmp_engine.transportDispatcher
            transportDispatcher.jobFinished(1)
            transportDispatcher.unregisterRecvCbFun(recvId=None)
            #transportDispatcher.unregisterTransport(udp.domainName)
            self.stop_threads(self.send_queue, send_threads)

def main():
    '''Main function to execute when the script is executed directly'''
    Main().run()

if __name__ == '__main__':
    main()
