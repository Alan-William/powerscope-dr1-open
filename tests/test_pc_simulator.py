import ctypes as ct
import hashlib
import math
from pathlib import Path
import struct
import tempfile
import time
import unittest

from pc_monitor.demo import Demo
from pc_monitor.simulator import CONDITIONS, LiveSimulation, NativePlant, normalized_window, runtime_status
from tests.test_pc_demo import FakeBoard, FakeMonitor
from tests.test_pc_monitor import wait_until


@unittest.skipUnless(runtime_status()[0], "Build the native circuit solver first")
class CircuitTests(unittest.TestCase):
    def plant(self, condition=0):
        plant = NativePlant(CONDITIONS[condition])
        self.addCleanup(plant.close)
        return plant

    def test_healthy_kcl_and_three_phase_balance(self):
        plant = self.plant()
        for _ in range(80):
            plant.advance()
        data = plant.advance(2000)
        currents = data["actual"]
        rms = [math.sqrt(sum(x*x for x in row)/len(row)) for row in currents]
        self.assertLess(max(rms)/min(rms), 1.03)
        self.assertLess(max(abs(sum(sample)) for sample in zip(*currents)), 2e-6)
        self.assertAlmostEqual(max(rms)*math.sqrt(2), data["nominal_peak_a"], delta=.15)
        crossings = [i for i in range(1,2000) if currents[0][i-1] <= 0 < currents[0][i]]
        periods = [b-a for a,b in zip(crossings,crossings[1:])]
        self.assertGreater(len(periods), 2)
        self.assertAlmostEqual(20000/(sum(periods)/len(periods)), 50, delta=.5)
        self.assertGreater(data["power_w"], 100)

    def test_continuous_state_independent_of_window_partition(self):
        first, second = self.plant(), self.plant()
        whole = first.advance(500)
        a, b = second.advance(200), second.advance(300)
        for key in ["actual", "measured", "voltage"]:
            self.assertEqual(whole[key], [a[key][k]+b[key][k] for k in range(3)])
        self.assertAlmostEqual(whole["solver_time_s"], .025, places=12)

    def test_sensor_faults_act_on_observation_and_keep_true_current(self):
        plant = self.plant()
        plant.advance(20000)
        plant.fault(13)
        disconnected = plant.advance()
        self.assertEqual(set(disconnected["measured"][0]), {0.0})
        self.assertGreater(max(map(abs,disconnected["actual"][0])), .1)
        plant.fault(16)
        first, second = plant.advance(), plant.advance()
        self.assertEqual(len(set(first["measured"][0]+second["measured"][0])), 1)
        plant.fault(19)
        offset = plant.advance()
        delta = .2*offset["nominal_peak_a"]
        self.assertTrue(all(abs(m-a-delta)<2e-6 for m,a in zip(offset["measured"][0],offset["actual"][0])))
        plant.fault(0)
        recovered = plant.advance()
        self.assertEqual(recovered["actual"], recovered["measured"])

    def test_disabled_igbt_preserves_its_antiparallel_diode(self):
        plant = self.plant()
        pole = plant.dll.npc_pole
        pole.argtypes = [ct.c_int,ct.c_int,ct.c_double,ct.c_double]
        pole.restype = ct.c_double
        # Open T1 blocks positive current from DC+, while reverse diode current remains.
        self.assertEqual(pole(1,1,2,50),0)
        self.assertEqual(pole(1,1,-2,50),50)
        self.assertEqual(pole(1,2,2,50),-50)
        self.assertEqual(pole(-1,4,-2,50),0)
        self.assertEqual(pole(-1,4,2,50),-50)
        self.assertEqual(pole(-1,3,-2,50),50)

    def test_all_faults_are_finite_in_four_configured_loads(self):
        for condition in range(4):
            plant = self.plant(condition)
            plant.advance(2000)
            for label in range(22):
                plant.fault(label)
                data = plant.advance(1000)
                self.assertTrue(all(math.isfinite(x) and abs(x)<500 for row in data["actual"] for x in row))
                self.assertLess(max(abs(sum(sample)) for sample in zip(*data["actual"])),1e-4)

    def test_normalization_is_joint_and_keeps_sensor_offset(self):
        waves = [[float(k+1)]*500 for k in range(3)]
        raw, scale = normalized_window(waves)
        values = struct.unpack("<1500f",raw)
        self.assertEqual(len(raw),6000)
        self.assertEqual(scale,3)
        self.assertAlmostEqual(values[0],1/3)
        self.assertEqual(values[-1],1)
        tiny, tiny_scale = normalized_window([[1e-12]*500]*3)
        self.assertEqual(tiny_scale, 1e-9)
        self.assertAlmostEqual(struct.unpack("<1500f",tiny)[0], .001)
        with self.assertRaises(ValueError):
            normalized_window([[math.nan]*500]*3)


@unittest.skipUnless(runtime_status()[0], "Build the native circuit solver first")
class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sim = LiveSimulation(self.root / "sim")

    def tearDown(self):
        self.sim.stop(wait=True)
        self.temp.cleanup()

    def test_real_time_pacing_fault_restore_and_complete_csv(self):
        self.sim.start()
        wait_until(lambda: bool(self.sim.snapshot()["latest"]))
        start_time = time.monotonic()
        first = self.sim.snapshot()["elapsed_s"]
        time.sleep(.3)
        second = self.sim.snapshot()["elapsed_s"]
        self.assertAlmostEqual(second-first,time.monotonic()-start_time,delta=.1)
        self.sim.select(16)
        wait_until(lambda:(self.sim.snapshot()["latest"] or {}).get("label")==16)
        raw, context = self.sim.window()
        self.assertEqual(len(raw),6000)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),context["sha256"])
        self.assertEqual(len(set(self.sim.snapshot()["latest"]["measured"][0])),1)
        self.sim.select(0)
        wait_until(lambda:(self.sim.snapshot()["latest"] or {}).get("label")==0)
        self.sim.stop(wait=True)
        self.assertEqual(self.sim.status,"stopped")
        stream, _, count = self.sim.export(self.sim.session)
        with stream:
            data = stream.read(count).decode("utf-8-sig")
        self.assertTrue(data.endswith("\n"))
        self.assertIn("window_sha256",data)
        self.assertGreater(len(data.splitlines()),5)
        with self.assertRaises(ValueError): self.sim.window()

    def test_live_sender_uses_samples_and_preserves_returned_prediction(self):
        self.sim.start()
        wait_until(lambda:bool(self.sim.snapshot()["latest"]))
        monitor = FakeMonitor()
        board = FakeBoard(monitor)
        old_state = board.state
        def new_state():
            value = old_state()
            value["external_input"]["display_metadata_version"] = 1
            return value
        board.state = new_state
        demo = Demo(monitor,self.root/"unused.json",self.root/"wire",board,simulator=self.sim)
        try:
            demo.start("test-port",source="live")
            wait_until(lambda:demo.snapshot()["matched"]>0)
            self.sim.select(2)
            wait_until(lambda:(demo.snapshot()["latest_match"] or {}).get("source_label")==2)
            demo.stop(wait=True)
            self.assertEqual(demo.status,"stopped")
            result = demo.snapshot()["latest_match"]
            self.assertEqual(result["label"],0)
            self.assertFalse(result["matches_reference"])
            for path, body in board.calls:
                if path == "/api/stream/window":
                    self.assertEqual(set(body),{"run_id","input_id","sha256","samples_b64","display"})
                    self.assertEqual(set(body["display"]),{"scale_a","sample_rate_hz","sim_time_s"})
                    self.assertGreater(body["display"]["scale_a"],0)
        finally:
            demo.stop(wait=True)


if __name__ == "__main__":
    unittest.main()
