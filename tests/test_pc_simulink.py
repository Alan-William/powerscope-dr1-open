import csv
import hashlib
import io
import json
from pathlib import Path
import socket
import struct
import tempfile
import unittest

from pc_monitor.simulink import COMMAND, HEADER, ROOT, SAMPLES, SimulinkSimulation, decode_packet
from tests.test_pc_monitor import wait_until


def packet(token, seq=1, label=0, kind=b"PSL1", command=0):
    header=HEADER.pack(kind,1,token,seq,label,command,0,seq*.025,seq*.025,23,0,4,240,0)
    return header + (SAMPLES.pack(*([1.0]*500+[-.5]*1000)*3) if kind==b"PSL1" else b"")


class SimulinkPacketTests(unittest.TestCase):
    def test_exact_binary_contract_and_phase_order(self):
        token=b"a"*16
        frame=decode_packet(packet(token),token)
        self.assertEqual(HEADER.size,96)
        self.assertEqual(len(packet(token)),18096)
        self.assertEqual(frame["measured"][0],[1.0]*500)
        self.assertEqual(frame["actual"][1],[-.5]*500)
        self.assertEqual(frame["sim_time_s"],.025)

    def test_rejects_cross_session_malformed_and_nonfinite_packets(self):
        token=b"a"*16; valid=packet(token)
        cases=[b"",valid[:-1],packet(b"b"*16),packet(token,label=22),valid[:96]+struct.pack('<f',float('nan'))+valid[100:]]
        for raw in cases:
            with self.subTest(size=len(raw)),self.assertRaises(ValueError): decode_packet(raw,token)


class SimulinkReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.runtime=self.root/'runtime';self.runtime.mkdir()
        hashes={}; source={}; digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
        for name in ('powerscope_bridge.c','powerscope_sensor.m','powerscope_build.m'):
            hashes[name.replace('.','_')]=digest(ROOT/'matlab'/name)
        snapshot=self.runtime/'research-copy/sim';snapshot.mkdir(parents=True)
        for name in ('npc_diag.slx','ctrl_npc.m','params_npc.m','sensor_fault.m'):
            path=snapshot/name;path.write_bytes(b'protocol-test-fixture');source[name.replace('.','_')]=digest(path)
        for name in ('powerscope_npc_live.slx','powerscope_bridge.mexw64'):
            (self.runtime/name).write_bytes(b'protocol-test-fixture')
        (self.runtime/'build.json').write_text(json.dumps(dict(test_fixture=True,implementation=hashes,source=source,
            model_sha256=digest(self.runtime/'powerscope_npc_live.slx'),bridge_sha256=digest(self.runtime/'powerscope_bridge.mexw64'))))
        self.sim=SimulinkSimulation(self.root/'output',self.runtime)
        self.config=self.sim.arm(duration_s=.1,warmup_s=0)
        self.peer=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);self.peer.bind(('127.0.0.1',0));self.peer.settimeout(1)
        self.address=('127.0.0.1',self.config['udp_port'])
        self.token=bytes.fromhex(self.config['session'])

    def tearDown(self):
        if self.sim.thread.is_alive():
            self.peer.sendto(packet(self.token,kind=b'PSE1'),self.address)
            self.sim.thread.join(1)
        self.sim.stop(wait=True);self.peer.close();self.temp.cleanup()

    def test_live_window_csv_and_exact_raw_record(self):
        raw=packet(self.token)
        self.peer.sendto(raw,self.address)
        wait_until(lambda:self.sim.snapshot()['latest'] is not None)
        window,context=self.sim.window()
        self.assertEqual(context['source_kind'],'npc-simulink-live-r1')
        self.assertEqual(len(window),6000)
        self.assertEqual(context['sha256'],hashlib.sha256(window).hexdigest())
        self.assertIsNone(self.sim.window(1))
        self.peer.sendto(packet(self.token,kind=b'PSE1'),self.address)
        self.sim.thread.join(1)
        handle,_,size=self.sim.export(self.sim.session)
        with handle: rows=list(csv.DictReader(io.StringIO(handle.read(size).decode('utf-8-sig'))))
        self.assertEqual(len(rows),1)
        self.assertEqual((self.root/'output'/(self.sim.session+'.windows.bin')).read_bytes(),raw)
        self.assertEqual(self.sim.status,'stopped')
        with self.assertRaises(ValueError):self.sim.window()

    def test_fault_command_is_session_bound_and_requires_model_ack(self):
        self.peer.sendto(packet(self.token),self.address)
        wait_until(lambda:self.sim.status=='running')
        self.sim.select(16)
        raw,_=self.peer.recvfrom(200)
        command=COMMAND.unpack(raw)
        self.assertEqual(command[:3],(b'PSC1',1,self.token))
        self.assertEqual(command[-2:],(1,16))
        self.assertEqual(self.sim.snapshot()['label'],0)
        self.peer.sendto(packet(self.token,2,label=16,command=command[3]),self.address)
        wait_until(lambda:self.sim.snapshot()['label']==16)
        self.assertIsNone(self.sim.pending_command)

    def test_gap_duplicate_and_wrong_session_are_visible(self):
        self.peer.sendto(packet(b'x'*16),self.address)
        self.peer.sendto(packet(self.token,3),self.address)
        wait_until(lambda:self.sim.snapshot()['latest'] is not None)
        self.peer.sendto(packet(self.token,2),self.address)
        self.assertEqual(self.sim.dropped_windows,2)
        self.assertEqual(self.sim.invalid_packets,1)
        self.assertEqual(self.sim.snapshot()['latest']['window_id'],3)


if __name__=='__main__':unittest.main()
