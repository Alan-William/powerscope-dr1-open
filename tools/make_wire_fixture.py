"""Deterministic protocol-only vectors; these are not model predictions."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from protocol.modbus import frame,read_request,reply
regs=[1,5,21,9876,0x1234,0x5678,0,900,0,0]
cases={'read_all':read_request(),'read_last':read_request(8,2),
    'out_of_range':read_request(9,2),'zero_count':frame(bytes.fromhex('010300000000')),
    'wrong_function':frame(bytes.fromhex('010600000001')),
    'broadcast_read':frame(bytes.fromhex('00030000000a')),
    'bad_crc':bytes.fromhex('01030000000ac5cc')}
output={'scope':'synthetic protocol vectors, not measured predictions','registers':regs,
    'cases':[{ 'name':name,'request_hex':req.hex(),
        'response_hex':None if reply(req,regs) is None else reply(req,regs).hex()} for name,req in cases.items()]}
p=Path(__file__).resolve().parents[1]/'fixtures/modbus_vectors.json'
p.write_text(json.dumps(output,indent=2)+'\n',encoding='utf-8')
print(p)
