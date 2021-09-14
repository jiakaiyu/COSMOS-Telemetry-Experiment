"""
Author: Jiakai Yu, Shengxiang Zhu
Code Goal: Telemetry Testing of transport system in COSMOS testbed
Date: 01.2020
"""


"""
Telemetry system scan spectrum to get channel power prediction and channel OSNR:
Calient(Telnet)
Lumentum(NETCONF)
ITLA(serial)

0. Initialize devices:
    0.1. Start SG985
    0.2. Turn on Scope
    0.3. Turn on AWG
    0.4. Connect ITLA
    0.5. Connect Lumentum

1. Measure continuous signal power at center frequency (ch. 48):
    1.1. Disconnect Telemetry Rx side connection in Calient
    1.2. Lumentum (only allow telemetry Tx signal go through link under test LUT):
        1.2.1. Add port, all channels set to port 4104
        1.2.2. Drop port, all channels set to port 5203
        1.2.4. Drop port, set ch. 48 to port 5204
        1.2.5. Intermediate nodes: set mux all channels to 4101
        1.2.6. Intermediate nodes: set demux all channels to 5201
    1.3. AWG: Set DC 0V output
    1.4. ITLA: turn on channel ch. 48
    1.5. Check power in Calient, connect drop port to Telemetry Rx side
    1.6. Scope: Capture amplitude, if too high, close ITLA raise error, else save
    1.7. Lumentum (open all Telemetry Tx channels, close all Telemetry Rx channels, allow signal go through LUT):
        1.7.1 Add port, all channels set to port 4104
        1.7.2 Add port, signal channels set to port 4101
        1.7.3 Drop port, all channels set to port 5203
        1.7.4 Drop port, signal channels set to port 5201

2. Scan ch.10, 20, ..., 90 and for each channel CH, do the following things:
    2.1. ITLA: turn on channel CH
    2.2. Lumentum: Drop node, set ch. CH to port 5204
    2.3. AWG: Set Pulse 95% duty ratio, 10000 Hz, 3500 mV Amplitude
    2.4. Scope:
        2.4.1. Set trigger half of max voltage
        2.4.2. Set time 5us/div and amplitude 500 mV/div
        2.4.3. Set probe 1x
        2.4.4. Set average 100 samples
        2.4.5. Take sample
        2.4.6. Extract peak and idle voltage
        2.4.7. Save data
    #   2.5. Lumentum: Drop node, set ch. CH back to port 5203

3. Process data
"""
import collections
import math

import ncclient
import serial
import struct
import clr
import re
import socket
import telnetlib
import time

clr.AddReference('AnalogArtsDataProcessor_DLL_4_24_2019/AnalogArtsDataProcessor')
clr.AddReference('AnalogArtsDataProcessor_DLL_4_24_2019/CyUSB')
from AnalogArtsDataProcessor import InitializeTheInstrument
from AnalogArtsDataProcessor import Scope, Generator


import xmltodict

from ncclient import manager
from ncclient.xml_ import to_ele


ROADM1 = '***.***.***.1'
ROADM2 = '***.***.***.2'
ROADM3 = '***.***.***.3'
ROADM4 = '***.***.***.4'
ROADM5 = '***.***.***.5'
ROADM6 = '***.***.***.6'
LUMENTUM_PORT = '830'

MUX = '1'
DEMUX = '2'
INSERVICE = 'in-service'


USERNAME = '***'
PASSWORD = '***'

HOST = "***.***.***.111"
PORT = "3082"
AUTH = "***;\n"

DEFAULT_WSS_LOSS = '4.0'
ZERO_WSS_LOSS = '0.0'


class Lumentum(object):

    class WSSConnection(object):

        def __init__(self,
                     module,
                     connection_id,
                     operation,
                     blocked,
                     input_port,
                     output_port,
                     start_freq,
                     end_freq,
                     attenuation,
                     name
                     ):
            self.module = module
            self.connection_id = connection_id
            self.operation = operation
            self.blocked = blocked
            self.input_port = input_port
            self.output_port = output_port
            self.start_freq = start_freq
            self.end_freq = end_freq
            self.attenuation = attenuation
            self.name = name

    class WSSConnectionStatus(WSSConnection):

        @classmethod
        def from_connection_details(cls, connection_details):
            return [
                cls(
                    connection_detail['dn'].split(';')[3].split('=')[1],
                    connection_detail['dn'].split(';')[4].split('=')[1],
                    connection_detail['config']['maintenance-state'],
                    connection_detail['config']['blocked'],
                    connection_detail['config']['input-port-reference'].split('port=')[1],
                    connection_detail['config']['output-port-reference'].split('port=')[1],
                    connection_detail['config']['start-freq'],
                    connection_detail['config']['end-freq'],
                    connection_detail['config']['attenuation'],
                    connection_detail['config']['custom-name'],
                    connection_detail['state']['input-channel-attributes']['power'],
                    connection_detail['state']['output-channel-attributes']['power'],
                    connection_detail['dn'].split(';')[0].split('=')[1],
                    connection_detail['dn'].split(';')[1].split('=')[1],
                    connection_detail['dn'].split(';')[2].split('=')[1]
                ) for connection_detail in connection_details['data']['connections']['connection'] if connection_detail
            ]

        def __init__(self,
                     module,
                     connection_id,
                     operation,
                     blocked,
                     input_port,
                     output_port,
                     start_freq,
                     end_freq,
                     attenuation,
                     name,
                     input_power,
                     output_power,
                     ne,
                     chassis,
                     card
                     ):
            super(Lumentum.WSSConnectionStatus, self).__init__(
                     module,
                     connection_id,
                     operation,
                     blocked,
                     input_port,
                     output_port,
                     start_freq,
                     end_freq,
                     attenuation,
                     name)
            self.input_power = input_power
            self.output_power = output_power
            self.ne = ne
            self.chassis = chassis
            self.card = card

    def __init__(self, IP_addr):
        self.m = manager.connect(host=IP_addr, port=LUMENTUM_PORT,
                                 username=USERNAME, password=PASSWORD,
                                 hostkey_verify=False)

    def __del__(self):
        self.m.close_session()

    def wss_delete_connection(self, module_id, connection_id):
        try:
            if connection_id == 'all':
                reply = self.m.dispatch(to_ele('''
                <remove-all-connections
                xmlns="http://www.lumentum.com/lumentum-ote-connection">
                <dn>ne=1;chassis=1;card=1;module=%s</dn>
                </remove-all-connections>
                ''' % module_id))
            else:
                reply = self.m.dispatch(to_ele('''
                <delete-connection xmlns="http://www.lumentum.com/lumentum-ote-connection">
                <dn>ne=1;chassis=1;card=1;module=%s;connection=%s</dn>
                </delete-connection>
                ''' % (module_id, connection_id)))
            if '<ok/>' in str(reply):
                print('Successfully Deleted Connection')
        except Exception as e:
            print("Encountered the following RPC error!")
            print(e)

    def wss_add_connections(self, connections):

        def gen_connection_xml(wss_connection):
            return '''<connection>
              <dn>ne=1;chassis=1;card=1;module=%s;connection=%s</dn>
              <config>
                <maintenance-state>%s</maintenance-state>
                <blocked>%s</blocked>
                <start-freq>%s</start-freq>
                <end-freq>%s</end-freq>
                <attenuation>%s</attenuation>
                <input-port-reference>ne=1;chassis=1;card=1;port=%s</input-port-reference>
                <output-port-reference>ne=1;chassis=1;card=1;port=%s</output-port-reference>
                <custom-name>%s</custom-name>
              </config> 
            </connection>''' % (
                wss_connection.module,
                wss_connection.connection_id,
                wss_connection.operation,
                wss_connection.blocked,
                wss_connection.start_freq,
                wss_connection.end_freq,
                wss_connection.attenuation,
                wss_connection.input_port,
                wss_connection.output_port,
                wss_connection.name
            )

        new_line = '\n'
        services = '''<xc:config xmlns:xc="urn:ietf:params:xml:ns:netconf:base:1.0">
        <connections xmlns="http://www.lumentum.com/lumentum-ote-connection" 
        xmlns:lotet="http://www.lumentum.com/lumentum-ote-connection">
            %s
        </connections>
        </xc:config>''' % new_line.join([gen_connection_xml(connection) for connection in connections])

        try:
            reply = self.m.edit_config(target='running', config=services)
            if '<ok/>' in str(reply):
                print('Successfully Added Connections')
        except Exception as e:
            print("Encountered the following RPC error!")
            print(e)

    def wss_get_connections(self):

        command = '''
                <filter>
                  <connections xmlns="http://www.lumentum.com/lumentum-ote-connection">
                  </connections>
                </filter>
                '''
        try:
            conn = self.m.get(command)
            connection_details = xmltodict.parse(conn.data_xml)
            # print(connection_details['data']['connections']['connection'])
        except Exception as e:
            print("Encountered the following RPC error!")
            print(e)
        connections = Lumentum.WSSConnectionStatus.from_connection_details(connection_details)
        return connections

    def wss_print_connections(self):
        connections = self.wss_get_connections()
        if not connections:
            print('No connection')
        else:
            for connection in connections:
                print(connection.__dict__)

    @staticmethod
    def gen_dwdm_connections(module, input_port, output_port,
                             channel_spacing=50.0, channel_width=50.0, loss=DEFAULT_WSS_LOSS):
        """
        :param module:
        :param input_port:
        :param output_port:
        :param channel_spacing: in GHz
        :param channel_width: in GHz
        :return:
        """
        connections = []
        half_channel_width = channel_width / 2.0  # in GHz
        start_center_frequency = 191350.0  # in GHz
        for i in range(96):
            center_frequency = start_center_frequency + i * channel_spacing
            connection = Lumentum.WSSConnection(
                module,
                str(i + 1),
                INSERVICE,
                'false',
                input_port,
                output_port,
                str(center_frequency - half_channel_width),
                str(center_frequency + half_channel_width),
                loss,
                'CH' + str(i + 1)
            )
            connections.append(connection)
        return connections


class PowerLimitError(Exception):
    """Custom exception for ITLA laser"""

    def __init__(self, message):
        self.message = message


class ChannelLimitError(Exception):
    """Custom exception for ITLA laser"""

    def __init__(self, message):
        self.message = message


class ITLA(object):
    """Manage ITLA laser


    port:COM port number
    timeout: timeout when reading
    baud: baudrate
    stopbits:stopbits
    name:__repr__

    Byte 1, checsum(4 bits)+ 000 + 0(Read) or 1 (Write)
    Byte 2, function register
    Byte 3, Data byte
    Byte 4, Data byte
    Function Register
    If want to set frequency a.b
    0x35 FCF1, set first channel frequency a(THZ)
    0x36 FCF2, set first channel frequency b(GHZ*10)
    0x31, set power level (val *0.01dBm)
    0x32, turn laser on/off
    0x30, set channel number
    """

    def __init__(self, port, timeout=0.01, baud=9600, stopbits=0, name=None):
        self.connection = serial.Serial(port, timeout=timeout)

    def __del__(self):
        self.close()

    def cal_checksum(self, *data):
        bip8 = (data[0] & 0x0f) ^ data[1] ^ data[2] ^ data[3]
        bip4 = ((bip8 & 0xf0) >> 4) ^ (bip8 & 0x0f)
        return bip4

    def laser_off(self):
        self.connection.write(bytearray([01, 50, 0, 0]))

    def laser_on(self):
        self.connection.write(bytearray([129, 50, 0, 8]))

    def set_first_channel_frequency(self, freq):  # in THz
        # TODO(WEIYANG): Custom Range for frequency above range
        FCF1 = int(math.floor(freq))
        B = struct.pack(">h", FCF1)
        (data0, data1, data2, data3) = (01, 53, ord(B[0]), ord(B[1]))  # 01:write,53:register
        bip4 = self.cal_checksum(data0, data1, data2, data3)
        data0 = data0 ^ (bip4 << 4)
        cmd1 = bytearray([0, 0, 0, 0])
        cmd1[0] = data0
        cmd1[1] = data1
        cmd1[2] = data2
        cmd1[3] = data3
        FCF2 = int(round((freq - FCF1), 3) * 10000)
        B = struct.pack(">h", FCF2)
        (data0, data1, data2, data3) = (01, 54, ord(B[0]), ord(B[1]))  # 01:write,54:register
        bip4 = self.cal_checksum(data0, data1, data2, data3)
        data0 = data0 ^ (bip4 << 4)
        cmd2 = bytearray([0, 0, 0, 0])
        cmd2[0] = data0
        cmd2[1] = data1
        cmd2[2] = data2
        cmd2[3] = data3
        self.connection.write(cmd1)
        time.sleep(0.01)
        self.connection.write(cmd2)

    def set_power(self, value):  # in dBm
        if value < 6 or value > 13.5:
            raise PowerLimitError("The power range is 6.0dBm-13.5dBm!")
        else:
            set_value = int(round(value, 2) * 100)
            B = struct.pack(">h", set_value)
            (data0, data1, data2, data3) = (01, 49, ord(B[0]), ord(B[1]))  # 01: write, 49: function register (0x31)
            bip4 = self.cal_checksum(data0, data1, data2, data3)
            data0 = data0 ^ (bip4 << 4)
            cmd = bytearray([0, 0, 0, 0])
            cmd[0] = data0
            cmd[1] = data1
            cmd[2] = data2
            cmd[3] = data3
            self.connection.write(cmd)

    def set_channel(self, num):
        B = struct.pack(">h", num)
        (data0, data1, data2, data3) = (01, 48, ord(B[0]), ord(B[1]))
        bip4 = self.cal_checksum(data0, data1, data2, data3)
        data0 = data0 ^ (bip4 << 4)
        cmd = bytearray([0, 0, 0, 0])
        cmd[0] = data0
        cmd[1] = data1

        cmd[2] = data2
        cmd[3] = data3
        self.connection.write(cmd)

    def close(self):
        self.connection.close()


## Scope test code
# print('initialize')
# print(InitializeTheInstrument.start())
# print('turn on scope, takes 30 seconds')
# print(Scope.turnOnTheOscilloscope())
# # print('load scope settings')
# # print(Scope.loadScopeSettings())
# print('read waveform')
# data = list(Scope.getCH1NewData())
# print(data)
# data = list(Scope.getCH1NewData())
# print(data)
# data = list(Scope.getCH1NewData())
# print(data)
# data = list()


## Generator test code
# print('turn on the generator, takes 30 seconds')
# print(Generator.turnOnTheGenerator())
# print(Generator.generatePulse())
# print(Generator.setAmplitude(3500))
# print(Generator.setSignalFrequency(10000))
# print(Generator.setDutyCycle(95))
#
# print(Generator.getGeneratedSignal())
# print(Generator.getAmplitude())
# print(Generator.getSignalFrequency())
# print(Generator.getDutyCycle())
# print(Generator.makeTheSignalContinuous())
# print(Generator.turnOnTheGenerator())

# input('press enter to continue')


def channel_freq(ch):
    return 191.30 + ch * 0.05


def calculate_predicted_channel_power_from_peak_voltage(
        peak_voltage,
        single_channel_probed_power,
        target_gain,
        pd_response,
        existing_channel_num
):
    """
    :param peak_voltage: measured probed peak voltage in mV for probed channel.
    :param single_channel_probed_power: measured probed power in dBm for single channel.
    :param target_gain: EDFA target gain in dB, typical value: 18 dB.
    :param pd_response: PD response in mV/mW, typical value: 14276.
    :param existing_channel_num: number of existing channels.
    :return: predicted channel power in dBm.
    """

    def linear_to_db(linear_power_value):
        if linear_power_value <= 0:
            raise ValueError("linear_power_value must be positive.")
        return 10 * math.log10(linear_power_value)

    def db_to_linear(dbm_power_value):
        return 10 ** (dbm_power_value / 10.0)

    peak_power_dbm = linear_to_db(peak_voltage / pd_response)
    single_channel_peak_power_dbm = single_channel_probed_power
    ripple = peak_power_dbm - single_channel_peak_power_dbm
    edfa_gain_linear = db_to_linear(target_gain + ripple)
    target_gain_linear = db_to_linear(target_gain)
    mean_gain_linear = (edfa_gain_linear + target_gain_linear * existing_channel_num) / (existing_channel_num + 1)
    mean_gain_db = linear_to_db(mean_gain_linear)
    excursion = target_gain - mean_gain_db
    predicted_channel_power = excursion + peak_power_dbm
    return predicted_channel_power


def test_channel_power_prediction():
    """
    The first parameter is from Telemetry measurement, the others are characterized data, which are manually measured.
    :return: None
    """
    print(calculate_predicted_channel_power_from_peak_voltage(488.0, -13.5, 18.0, 14276.0, 1))



################## CALINET TELNET ##########

def open_connection(host, port, auth):
    tn = telnetlib.Telnet(host, port)
    tn.write(auth)
    return tn


def close_connect(tn):
    tn.get_socket().shutdown(socket.SHUT_WR)
    data = tn.read_all()
    tn.close()
    return data


def get_crs_power(tn, _port):
    port=[]
    pattern1='[1-9].[1-9].[1-9][>-][1-9].[1-9].[1-9]'
    pattern2='INPWR=[+-]?\d+(?:\.\d+)?'
    pattern3='OUTPWR=[+-]?\d+(?:\.\d+)?'
    tn.write("RTRV-PORT-SUM::{};\n".format(_port))
    time.sleep(0.5)
    port_data=tn.read_very_eager()
    crs = re.search(pattern1,port_data)
    input_power = re.search(pattern2, port_data)
    output_power = re.search(pattern3, port_data)
    if crs!=None:
        port.append(crs.group(0))
    if input_power!=None:
        port.append(input_power.group(0))
    if output_power!=None:
        port.append(output_power.group(0))
    return port


def calient_get_power(port):
    s320 = open_connection(HOST, PORT, AUTH)
    crs_power = get_crs_power(s320, port)
    close_connect(s320)
    return crs_power


# unidirectional crs: "a>b"	bi-directional crs: "a-b"
def Add_CRS_Calient(HOST, PORT, AUTH, crs):
    #print "Adding ports to Calient switch. Port 1 = " + str(crs)
    tn = telnetlib.Telnet(HOST, PORT)
    tn.write(AUTH)
    time.sleep(0.2)
    tn.write("ent-crs-bulk::{};\n".format(crs))
    time.sleep(0.2)
    tn.close()

# unidirectional crs: "a>b"	bi-directional crs: "a-b"
def Del_CRS_Calient(HOST, PORT, AUTH, crs):
    #print "Deleting ports from Calient switch. Port 1 = " + str(crs)
    tn = telnetlib.Telnet(HOST, PORT)
    tn.write(AUTH)
    time.sleep(0.2)
    tn.write("dlt-crs-bulk::{};\n".format(crs))
    time.sleep(0.2)
    tn.close()


######################### CONTROL PARAMETERS ################
    
# R1 -> R2
# TELEMETRY_TX_CONNECTION = "5.8.6>1.1.4"
# TELEMETRY_RX_CONNECTION = "1.4.4>5.8.6"
# TELEMETRY_RX_PARKING = "1.4.4>6.1.1"
# ADD_NODE = ROADM1
# DROP_NODE = ROADM2
# POWER_CHECK_PORT = "1.4.4"
# TELEMETRY_ADD_PORT_LOSS = "4.0"

# R2 -> R6
TELEMETRY_TX_CONNECTION = "5.8.6>1.4.4"
TELEMETRY_RX_CONNECTION = "1.2.4>5.8.6"
TELEMETRY_RX_PARKING = "1.2.4>6.1.1"
ADD_NODE = ROADM2
DROP_NODE = ROADM6
POWER_CHECK_PORT = "1.2.4"
TELEMETRY_ADD_PORT_LOSS = "18.0"

POWER_CHECK_PORT_TELEMETRY_TX = "5.8.6"

COM_FOR_ITLA = 'COM4'


INTERMEDIATE_NODES = []

MIDDLE_CHANNEL = 48  # no overlap with probe channels
CHANNEL_HALF_WIDTH = 25  # in GHz
VOLTAGE_SHRESHOLD = 1000  # in mV
PD_POWER_SHRESHOLD = -10  # in dBm
SIGNAL_CHANNELS_4101 = [25, 35, 45, 44]  # in Channel index
SIGNAL_CHANNELS_4102 = [22, ]  # in Channel index


# raw_input('Please make sure that ALS of all OAs are disabled. Press ENTER to continue.')
# raw_input('Please configure Calient switch, connect Telemetry Tx to add port and drop port to Telemetry Rx. '
#           'Press ENTER to continue.')
Add_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_TX_CONNECTION)
time.sleep(5)
Del_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_RX_CONNECTION)
time.sleep(5)
Add_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_RX_PARKING)
time.sleep(5)
# raw_input('Please turn off GUIs for ITLA and SG985. Press ENTER to continue.')

# roadm = Lumentum(ROADM1)
# connections = roadm.wss_get_connections()



#################################################################################
################################## START ########################################

## 0. Initialize devices
# 0.1. Start SG985
print('0.1. Start SG985 (takes 30 seconds for calibration)')
InitializeTheInstrument.start()
# 0.2. Turn on Scope
print('0.2. Turn on scope')
# Scope.turnOnTheOscilloscope()
# 0.3. Turn on AWG
print('0.3. Turn on AWG')
# Generator.turnOnTheGenerator()
# 0.4. Connect ITLA
print('0.4. Connect ITLA')
# itla = ITLA(COM_FOR_ITLA)
# 0.5. Connect Lumentum, set add/drop ports to parking port
print('0.5. Connect Lumentum')
try:
    r_add = Lumentum(ADD_NODE)
    r_drop = Lumentum(DROP_NODE)
    r_intermediate = [Lumentum(node) for node in INTERMEDIATE_NODES]
except Exception:
    print('Lumentum connection error. Try again.')
    time.sleep(5)
    r_add = Lumentum(ADD_NODE)
    r_drop = Lumentum(DROP_NODE)
    r_intermediate = [Lumentum(node) for node in INTERMEDIATE_NODES]

# 0.6. Disable all ALS
print('0.6. Disable all ALS')
_ROADM_IP_1 = ['***.***.***.1', '***.***.***.2', '***.***.***.3', '***.***.***.4', '***.***.***.5', '***.***.***.6', '***.***.***.21',
               '***.***.***.22', '***.***.***.23']
_ROADM_IP_2 = ['***.***.***.21', '***.***.***.22', '***.***.***.23']


def ALS(IP, module):
    try:
        m = manager.connect(host=IP, port=830,
                            username=USERNAME, password=PASSWORD,
                            hostkey_verify=False)

        service1 = '''<disable-als xmlns="http://www.lumentum.com/lumentum-ote-edfa"><dn>ne=1;chassis=1;card=1;edfa=1</dn><timeout-period>600</timeout-period></disable-als>'''
        service2 = '''<disable-als xmlns="http://www.lumentum.com/lumentum-ote-edfa"><dn>ne=1;chassis=1;card=1;edfa=2</dn><timeout-period>600</timeout-period></disable-als>'''
        if module == 1:
            service = service1
        else:
            service = service2
            rpc_reply = m.dispatch(to_ele(service))
    except Exception as e:
        print("Encountered the following RPC error!")
        print(e)
    finally:
        if m:
            m.close_session()


for IP1 in _ROADM_IP_1:
    print(IP1)
    # ALS(IP1, 1)
for IP2 in _ROADM_IP_2:
    print(IP2)
    # ALS(IP2, 2)


## 1. Measure continuous signal power at center freq (ch. 48):
print('1. Measure continuous signal power at center freq (ch. 48)')
# 1.1. Disconnect Telemetry Rx side connection in Calient
print('1.1. Disconnect Telemetry Rx side connection in Calient')
# raw_input('Please disconnect Telemetry Rx side connection in Calient. Press ENTER to continue.')
Del_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_RX_CONNECTION)
Add_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_RX_PARKING)
# 1.2. Lumentum (only allow telemetry Tx signal go through link under test LUT):
print('1.2. Lumentum (only allow telemetry Tx signal go through link under test LUT)')
#   1.2.1. Add port, all channels set to port 4104
print('1.2.1. Add port, all channels set to port 4104, use TELEMETRY_ADD_PORT_LOSS')
r_add.wss_delete_connection(MUX, 'all')
connections = r_add.gen_dwdm_connections(MUX, '4104', '4201', loss=TELEMETRY_ADD_PORT_LOSS)
r_add.wss_add_connections(connections)
#   1.2.2. Drop port, all channels set to port 5203
print('1.2.2. Drop port, all channels set to port 5203')
r_drop.wss_delete_connection(DEMUX, 'all')
connections = r_drop.gen_dwdm_connections(DEMUX, '5101', '5203')
r_drop.wss_add_connections(connections)
#   1.2.4. Drop port, set ch. 48 to port 5204
print('1.2.4. Drop port, set ch. 48 to port 5204')
r_drop.wss_delete_connection(DEMUX, MIDDLE_CHANNEL)
print((DEMUX, str(MIDDLE_CHANNEL), INSERVICE, 'false', '5101', '5204',
                                    str(channel_freq(MIDDLE_CHANNEL) * 1000 - CHANNEL_HALF_WIDTH),
                                    str(channel_freq(MIDDLE_CHANNEL) * 1000 + CHANNEL_HALF_WIDTH),
                                    DEFAULT_WSS_LOSS, 'CH' + str(MIDDLE_CHANNEL)))
connection = Lumentum.WSSConnection(DEMUX, str(MIDDLE_CHANNEL), INSERVICE, 'false', '5101', '5204',
                                    str(channel_freq(MIDDLE_CHANNEL) * 1000 - CHANNEL_HALF_WIDTH),
                                    str(channel_freq(MIDDLE_CHANNEL) * 1000 + CHANNEL_HALF_WIDTH),
                                    DEFAULT_WSS_LOSS, 'CH' + str(MIDDLE_CHANNEL))
r_drop.wss_add_connections([connection])
#   1.2.5. Intermediate nodes: set mux all channels to 4101
print('1.2.5. Intermediate nodes: set mux all channels to 4101')
for roadm in r_intermediate:
    roadm.wss_delete_connection(MUX, 'all')
    connections = roadm.gen_dwdm_connections(MUX, '4101', '4201')
    roadm.wss_add_connections(connections)
#   1.2.6. Intermediate nodes: set demux all channels to 5201
print('1.2.6. Intermediate nodes: set demux all channels to 5201')
for roadm in r_intermediate:
    roadm.wss_delete_connection(DEMUX, 'all')
    connections = roadm.gen_dwdm_connections(DEMUX, '5101', '5201')
    roadm.wss_add_connections(connections)
# 1.3. AWG: Set DC 0V output
print('1.3. AWG: Set DC 0V output')
Generator.turnOffTheGenerator()
# 1.4. ITLA: turn on channel ch. 48
print('1.4. ITLA: turn on channel ch. 48')
Del_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_TX_CONNECTION)
Add_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_TX_CONNECTION)
itla = ITLA(COM_FOR_ITLA)
time.sleep(10)
itla.laser_off()
time.sleep(10)
itla.set_power(7)
time.sleep(10)
itla.set_first_channel_frequency(channel_freq(MIDDLE_CHANNEL))
print(channel_freq(MIDDLE_CHANNEL))
time.sleep(10)
itla.close()
itla = ITLA(COM_FOR_ITLA)
itla.laser_on()
time.sleep(20)
itla.close()
power_reading = calient_get_power(POWER_CHECK_PORT_TELEMETRY_TX)
print(power_reading)
power_reading_count = 1
while len(power_reading) <= 1:
    print('power reading error, try again.')
    Del_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_TX_CONNECTION)
    Add_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_TX_CONNECTION)
    time.sleep(5)
    power_reading = calient_get_power(POWER_CHECK_PORT_TELEMETRY_TX)
    power_reading_count += 1
    if power_reading_count == 5:
        raw_input('Laser is not turned on properly. Please turn it on.'
                  'Press ENTER to continue.')
        power_reading = calient_get_power(POWER_CHECK_PORT_TELEMETRY_TX)
        break
r1_input_power = float(power_reading[1].lstrip('INPWR='))
print('ROADM add port telemetry input laser power: ', r1_input_power)
while r1_input_power < -10:
    print('Laser is not turned on properly. Try again.')
    itla = ITLA(COM_FOR_ITLA)
    time.sleep(10)
    itla.set_first_channel_frequency(channel_freq(MIDDLE_CHANNEL))
    print(channel_freq(MIDDLE_CHANNEL))
    time.sleep(10)
    itla.laser_on()
    time.sleep(20)
    itla.close()
    power_reading = calient_get_power(POWER_CHECK_PORT_TELEMETRY_TX)
    r1_input_power = float(power_reading[1].lstrip('INPWR='))
print('check MIDDLE CHANNEL power')
power_reading = [connection for connection in r_add.wss_get_connections() if str(connection.connection_id) == str(MIDDLE_CHANNEL)][0].input_power
print('MIDDLE CHANNEL power is: ', power_reading)
# 1.5. Check power in Calient, connect drop port to Telemetry Rx side
print('1.5. Check power in Calient, connect drop port to Telemetry Rx side')
# raw_input('Please check the power into the Telemetry Rx in Calient.'
#           'Connect drop port to Telemetry Rx side. '
#           'Press ENTER to continue.')
time.sleep(5)
power_reading = calient_get_power(POWER_CHECK_PORT)
print(power_reading)
power_reading_count = 1
while len(power_reading) == 1:
    print('power reading error, try again.')
    time.sleep(5)
    power_reading = calient_get_power(POWER_CHECK_PORT)
    print(power_reading)
    power_reading_count += 1
    print(power_reading_count)
    if power_reading_count == 10:
        raw_input('Laser is not turned on properly. Please turn it on.'
                  'Press ENTER to continue.')
        power_reading = calient_get_power(POWER_CHECK_PORT)
        break
r2_output_power = float(calient_get_power(POWER_CHECK_PORT)[1].lstrip('INPWR='))
print('R2 output power: ', r2_output_power)
if r2_output_power > -10:
    raise ValueError('PD power too high')
# if output_power > PD_POWER_SHRESHOLD:
#     raise ValueError('PD power input is above threhold (step 1.5)')
Del_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_RX_PARKING)
Add_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_RX_CONNECTION)

# raw_input('Please check Telemetry Rx connection, see if there is enough power. Press ENTER to continue.')

# 1.6. Scope: Capture amplitude, if too high, close ITLA raise error, else save
print('1.6. Scope: Capture amplitude, if too high, close ITLA raise error, else save')
Scope.turnOnTheOscilloscope()
Scope.makeMeaurementsAutomatic()
time.sleep(5)
Scope.setNumberOfDataToBeAveraged(100)
time.sleep(15)
print('read waveform')
data = list(Scope.getCH1NewData())
print(data)
print(sum(data) / len(data))
single_channel_probed_power = sum(data) / len(data)
print(single_channel_probed_power)
if single_channel_probed_power > VOLTAGE_SHRESHOLD:
    raise ValueError('single_channel_probed_power is above threhold (step 1.6)')
Scope.turnOffTheOscilloscope()
#     1.7. Lumentum (open all Telemetry Tx channels, close all Telemetry Rx channels, allow signal go through LUT):
print('1.7. Lumentum (open all Telemetry Tx channels, close all Telemetry Rx channels, allow signal go through LUT):')
#         1.7.1 Add port, all channels set to port 4104
print('1.7.1 Add port, all channels set to port 4104')
r_add.wss_delete_connection(MUX, 'all')
connections = r_add.gen_dwdm_connections(MUX, '4104', '4201', loss=TELEMETRY_ADD_PORT_LOSS)
r_add.wss_add_connections(connections)
#         1.7.2 Add port, signal channels set to port 4101
print('1.7.2 Add port, signal channels set to port 4101')
for channel in SIGNAL_CHANNELS_4101:
    connection = Lumentum.WSSConnection(MUX, str(channel), INSERVICE, 'false', '4101', '4201',
                                        str(channel_freq(channel) * 1000 - CHANNEL_HALF_WIDTH),
                                        str(channel_freq(channel) * 1000 + CHANNEL_HALF_WIDTH),
                                        DEFAULT_WSS_LOSS, 'CH' + str(channel))
    r_add.wss_add_connections([connection])
#         1.7.2 Add port, comb channels set to port 4102
print('1.7.2 Add port, comb channels set to port 4102')
for channel in SIGNAL_CHANNELS_4102:
    connection = Lumentum.WSSConnection(MUX, str(channel), INSERVICE, 'false', '4102', '4201',
                                        str(channel_freq(channel) * 1000 - CHANNEL_HALF_WIDTH),
                                        str(channel_freq(channel) * 1000 + CHANNEL_HALF_WIDTH),
                                        DEFAULT_WSS_LOSS, 'CH' + str(channel))
    r_add.wss_add_connections([connection])
#         1.7.3 Drop port, all channels set to port 5203
print('1.7.3 Drop port, all channels set to port 5203')
r_add.wss_delete_connection(DEMUX, 'all')
connections = r_add.gen_dwdm_connections(DEMUX, '5101', '5203')
r_add.wss_add_connections(connections)

#         1.7.4 Drop port, signal channels set to port 5201
print('1.7.4 Drop port, signal channels set to port 5201')
for channel in SIGNAL_CHANNELS_4101 + SIGNAL_CHANNELS_4102:
    connection = Lumentum.WSSConnection(DEMUX, str(channel), INSERVICE, 'false', '5101', '5201',
                                        str(channel_freq(channel) * 1000 - CHANNEL_HALF_WIDTH),
                                        str(channel_freq(channel) * 1000 + CHANNEL_HALF_WIDTH),
                                        DEFAULT_WSS_LOSS, 'CH' + str(channel))
    r_drop.wss_add_connections([connection])

Del_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_RX_PARKING)
time.sleep(5)
Add_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_RX_CONNECTION)
time.sleep(5)

# raw_input('Please check signal connection, see if they go throught the LUT. Press ENTER to continue.')

# 2. Scan ch.10, 20, ..., 90 and for each channel CH, do the following things:
print('2. Scan ch.10, 20, ..., 90 and for each channel CH, do the following things:')
scan_data = collections.OrderedDict()

# move AWG configuration here, don't have to repeat in each iteration.
#   2.3. AWG: Set Pulse 95% duty ratio, 10000 Hz, 3500 mV Amplitude
print('2.3. AWG: Set Pulse 95% duty ratio, 10000 Hz, 3500 mV Amplitude')
# Generator.turnOffTheGenerator()
# time.sleep(5)

# one time execution

# Generator.turnOnTheGenerator()
# time.sleep(5)
# raw_input('Please check the probe signal power. Press ENTER to continue.')


def calculate_idle_voltage(data, trigger_level):
    data = filter(lambda x: x < trigger_level, data)
    return sum(data) / len(data)


def calculate_peak_voltage(data, trigger_level):
    data = filter(lambda x: x > trigger_level, data)
    return sum(data) / len(data)


for channel in [x * 10 for x in range(1, 10)]:
    print('Current scanned channel: ', channel)
    #   2.1. ITLA: turn on channel CH
    print('2.1. ITLA: turn on channel CH (takes 30 seconds)')
    itla = ITLA('com4')
    itla.laser_off()
    time.sleep(5)
    itla.close()
    time.sleep(1)
    itla = ITLA('com4')
    time.sleep(1)
    itla.set_first_channel_frequency(channel_freq(channel))
    print(channel_freq(channel))
    time.sleep(5)
    itla.close()
    time.sleep(1)
    itla = ITLA('com4')
    time.sleep(1)
    itla.laser_on()
    time.sleep(20)
    itla.close()
    power = [connection for connection in r_add.wss_get_connections() if str(connection.connection_id) == str(channel)][0]
    print('power: ', power.connection_id, power.input_power)
    while float(power.input_power) < -30:
        print('ITLA enable failed. Cannot detect power from Lumentum')
        Del_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_TX_CONNECTION)
        time.sleep(5)
        Add_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_TX_CONNECTION)
        time.sleep(5)
        itla = ITLA('com4')
        time.sleep(10)
        itla.laser_off()
        time.sleep(10)
        itla.set_first_channel_frequency(channel_freq(channel))
        print(channel_freq(channel))
        time.sleep(10)
        print('turning itla laser on')
        itla.laser_on()
        time.sleep(20)
        itla.close()
        power = [connection for connection in r_add.wss_get_connections() if str(connection.connection_id) == str(channel)][0]
        print('power: ', power.connection_id, power.input_power)
    Generator.turnOnTheGenerator()
    Generator.generatePulse()
    time.sleep(5)
    Generator.setAmplitude(3500)
    time.sleep(5)
    Generator.setSignalFrequency(10000)
    time.sleep(5)
    Generator.setDutyCycle(95)
    time.sleep(5)

    print(Generator.getGeneratedSignal())
    print(Generator.getAmplitude())
    print(Generator.getSignalFrequency())
    print(Generator.getDutyCycle())
    print(Generator.makeTheSignalContinuous())

    print(Generator.getDutyCycle())
    # Del_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_TX_CONNECTION)
    # time.sleep(5)
    # Add_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_TX_CONNECTION)
    # time.sleep(5)
    #   2.2. Lumentum: Drop node, set ch. CH to port 5204
    print('2.2. Lumentum: Drop node, set ch. CH to port 5204')
    connection = Lumentum.WSSConnection(DEMUX, str(channel), INSERVICE, 'false', '5101', '5204',
                                        str(channel_freq(channel) * 1000 - CHANNEL_HALF_WIDTH),
                                        str(channel_freq(channel) * 1000 + CHANNEL_HALF_WIDTH),
                                        ZERO_WSS_LOSS, 'CH' + str(channel))
    r_drop.wss_add_connections([connection])

    # raw_input('Debug Scope here.')

    #   2.4. Scope:
    print('2.4. Scope:')
    # time.sleep(5)
    # Scope.resetDataCaptureCount()
    #    2.4.2. Set time 5us/div and amplitude 500 mV/div
    print('2.4.2. Set time 5us/div and amplitude 500 mV/div')
    Scope.turnOnTheOscilloscope()
    Scope.setCH1VoltsPerDiv(0.2)
    Scope.setTimePerDiv(5000)
    Scope.triggerOnCH1()
    Scope.setCH1Probe(1)
    Scope.setCH1Offset(0)
    Scope.DCcoupleCH1()
    Scope.setNumberOfDataPoints(5000)
    print('CH1 Probe: ', Scope.getCH1Probe())
    Scope.setTriggerThreshold(single_channel_probed_power / 2.0)
    Scope.setNumberOfDataToBeAveraged(100)  # TODO: Check this line
    time.sleep(2)
    trigger_level = float(str(Scope.getTriggerThreshold()).split()[0])
    print('trigger level (V): ', trigger_level)
    Scope.resetDataCaptureCount()
    time.sleep(10)
    # Scope.makeMeaurementsAutomatic()  # TODO: Check this line
    #    2.4.1. Set trigger half of max voltage
    print('2.4.1. Set trigger half of max voltage')
    # Scope.autoTrigger()  # TODO: Check this line
    #    plus: check voltage to make sure signal is in
    # current_max_voltage = max(list(Scope.getCH1NewData()))
    # raw_input('peek signal: too low. DEBUG')
    # while current_max_voltage < 0.01:
    #     print('peek signal: too low, wait for 5 seconds and try again.')
    #     time.sleep(5)
    #     current_max_voltage = max(list(Scope.getCH1NewData()))
    #    2.4.3. Set probe 1x
    print('2.4.3. Set probe 1x')  # TODO: Check this line
    pass
    # time.sleep(5)
    #    2.4.4. Set average 100 samples
    print('2.4.4. Set average 100 samples')
    # Scope.resetDataCaptureCount()
    # Scope.setNumberOfDataToBeAveraged(100)  # TODO: Check this line
    #    2.4.5. Take sample
    print('2.4.5. Take sample')
    # Del_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_TX_CONNECTION)
    # time.sleep(5)
    # Add_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_TX_CONNECTION)
    # time.sleep(5)
    for _ in range(200):
        Scope.getCH1NewData()
    data = list(Scope.getCH1NewData())
    print(data)
    # idle_voltage = Scope.measureCH1MinimumVoltage()  # TODO: Check this line
    # peak_voltage = Scope.measureCH1MaximumVoltage()  # TODO: Check this line
    idle_voltage = min(data)  # TODO: Check this line
    peak_voltage = max(data)  # TODO: Check this line
    while peak_voltage < trigger_level or idle_voltage > trigger_level:
        print('capture signal: exception.', peak_voltage, trigger_level, idle_voltage)
        Del_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_TX_CONNECTION)
        time.sleep(5)
        Add_CRS_Calient(HOST, PORT, AUTH, TELEMETRY_TX_CONNECTION)
        time.sleep(5)
        Scope.setCH1VoltsPerDiv(1)
        Scope.setTimePerDiv(10000)
        Scope.triggerOnCH1()
        Scope.autoTrigger()
        Scope.setNumberOfDataToBeAveraged(100)  # TODO: Check this line
        data = list(Scope.getCH1NewData())
        print(data)
        idle_voltage = min(data)
        peak_voltage = max(data)
        print(idle_voltage, peak_voltage)

    idle_voltage = calculate_idle_voltage(data, trigger_level)  # TODO: Check this line
    peak_voltage = calculate_peak_voltage(data, trigger_level)  # TODO: Check this line
    #    2.4.6. Extract peak and idle voltage
    print('2.4.6. Extract peak and idle voltage')
    print('peak voltage: ', peak_voltage)
    print('idle voltage: ', idle_voltage)
    Scope.turnOffTheOscilloscope()
    Generator.turnOffTheGenerator()
    #    2.4.7. Save data
    print('2.4.7. Save data')
    scan_data[channel] = [peak_voltage, idle_voltage]

    #   2.5. Lumentum: Drop node, set ch. CH back to port 5203
    print('2.2. Lumentum: Drop node, set ch. CH back to port 5203')
    connection = Lumentum.WSSConnection(DEMUX, str(channel), INSERVICE, 'false', '5101', '5203',
                                        str(channel_freq(channel) * 1000 - CHANNEL_HALF_WIDTH),
                                        str(channel_freq(channel) * 1000 + CHANNEL_HALF_WIDTH),
                                        DEFAULT_WSS_LOSS, 'CH' + str(channel))
    r_drop.wss_add_connections([connection])

print(scan_data)
    ## Generator test code
    # print('turn on the generator, takes 30 seconds')
    # print(Generator.turnOnTheGenerator())
    # print(Generator.generatePulse())
    # print(Generator.setAmplitude(3500))
    # print(Generator.setSignalFrequency(10000))
    # print(Generator.setDutyCycle(95))
    # print(Generator.getGeneratedSignal())
    # print(Generator.getAmplitude())
    # print(Generator.getSignalFrequency())
    # print(Generator.getDutyCycle())
    # print(Generator.makeTheSignalContinuous())
    # print(Generator.turnOnTheGenerator())

    # input('press enter to continue')

num_of_existing_channels = raw_input(
    'Please configure Lumentum to unblock signal channel (no overlap with probing channels). '
    'Enter the number of unblocked channels'
)

test_channel_power_prediction()
