"""Wire reference for the PL-owned Modbus RTU function 03 slave."""
import struct

def crc16(data):
    crc=0xffff
    for byte in data:
        crc ^= byte
        for _ in range(8): crc=(crc>>1)^0xa001 if crc&1 else crc>>1
    return crc

def frame(payload): return payload+struct.pack('<H',crc16(payload))

def read_request(start=0,count=10,address=1):
    if not 1<=address<=247 or not 1<=count<=125 or not 0<=start<=65535 or start+count>65536:
        raise ValueError('Invalid Modbus read request')
    return frame(struct.pack('>BBHH',address,3,start,count))

def reply(request,registers,address=1):
    """None means silent discard. Registers must be an atomic snapshot."""
    if len(request)<4 or request[0]!=address or crc16(request[:-2])!=int.from_bytes(request[-2:],'little'):
        return None
    if request[1]!=3: return frame(bytes([address,0x80|request[1],1]))
    if len(request)!=8: return None
    start,count=struct.unpack('>HH',request[2:6])
    if not 1<=count<=125: return frame(bytes([address,0x83,3]))
    if start+count>len(registers): return frame(bytes([address,0x83,2]))
    return frame(bytes([address,3,count*2])+struct.pack('>'+'H'*count,*registers[start:start+count]))

def decode_response(response,count=10,address=1):
    if len(response)<5 or crc16(response[:-2])!=int.from_bytes(response[-2:],'little'):
        raise ValueError('Invalid length or CRC')
    if response[0]!=address: raise ValueError('Wrong slave address')
    if response[1]&0x80: raise ValueError(f'Modbus exception {response[2]}')
    if response[1]!=3 or response[2]!=2*count or len(response)!=5+2*count:
        raise ValueError('Wrong function or register count')
    return struct.unpack('>'+'H'*count,response[3:-2])
