import unittest
from protocol.modbus import crc16,frame,read_request,reply,decode_response

class WireTests(unittest.TestCase):
    def test_known_crc(self):
        self.assertEqual(crc16(b'123456789'),0x4b37)
        self.assertEqual(read_request(0,10).hex(),'01030000000ac5cd')
    def test_snapshot_roundtrip(self):
        regs=[1,5,21,9876,0x1234,0x5678,0,900,0,0]
        self.assertEqual(decode_response(reply(read_request(),regs)),tuple(regs))
    def test_bad_crc_and_broadcast_silent(self):
        req=read_request(); self.assertIsNone(reply(req[:-1]+bytes([req[-1]^1]),[0]*10))
        self.assertIsNone(reply(frame(bytes.fromhex('00030000000a')),[0]*10))
    def test_bad_range_and_quantity(self):
        self.assertEqual(reply(read_request(9,2),[0]*10),frame(bytes.fromhex('018302')))
        self.assertEqual(reply(frame(bytes.fromhex('010300000000')),[0]*10),frame(bytes.fromhex('018303')))
    def test_unsupported_and_truncated(self):
        self.assertEqual(reply(frame(bytes.fromhex('010600000001')),[0]*10),frame(bytes.fromhex('018601')))
        with self.assertRaises(ValueError): decode_response(bytes.fromhex('010300'))

if __name__=='__main__': unittest.main()
