import math
import utils
import os.path
import re
import time
import sys
import array
import ctypes
import testprofile
import decoders as dec
import descriptors as desc
import terminalsize
import message_struct as ms
import flash_struct as fs
from message_struct import TargetPacketError
from serial import SerialException
import comport
try:
    import usb
except ImportError:
    usb = None

from datetime import datetime
from blinker import signal
from pkg_resources import Requirement, resource_filename
import logging
logging.getLogger('Devices').addHandler(logging.NullHandler())
from functools import wraps
from termcolor import cprint, colored

FLASH_BUFFER_LENGTH = 128

def retry_datalog(fn):
    """
    Retrys the decorated method on failure, logging details to database
    """

    @wraps(fn)
    def wrapped(*args, **kwargs):
        add_log = signal('datalog_add')
        caller = args[0]
        tries = caller.get_retries() + 1
        for retry in range(tries):
            not_last_retry = (retry != tries-1)
            dt_now = datetime.utcnow()
            (status, ret) = fn(*args, **kwargs)
            if(status == 0x01):
                if caller.log_errors_only:
                    pass
                else:
                    add_log.send(caller['mac'], name=fn.__name__, exp='0x%.2X'%0x01, act="0x%.2X"%status, cmd=1, retry=0, timestamp=str(dt_now))
                break
            else:
                add_log.send(caller['mac'], name=fn.__name__, exp='0x%.2X'%0x01, act="0x%.2X"%status, cmd=1, retry=int(not_last_retry), timestamp=str(dt_now))
        return (status, ret)
    return wrapped

def retry(fn):
    """
    Retrys the decorated method on failure
    """

    @wraps(fn)
    def wrapped(*args, **kwargs):
        caller = args[0]
        tries = caller.get_retries() + 1
        for retry in range(tries):
            (status, ret) = fn(*args, **kwargs)
            if(status == 0x01):
                break
        return (status, ret)
    return wrapped

def datalog(fn):
    """
    Logs details of the decorated method to database
    """
    @wraps(fn)
    def wrapped(*args, **kwargs):
        not_last_retry = 0
        add_log = signal('datalog_add')
        caller = args[0]
        dt_now = datetime.utcnow()
        (status, ret) = fn(*args, **kwargs)
        if caller.log_errors_only and (status == 0x01):
            pass
        else:
            add_log.send(caller['mac'], name=fn.__name__, exp='0x%.2X'%0x01, act='0x%.2X'%status, cmd=1, retry=0, timestamp=str(dt_now))

        return (status, ret)
    return wrapped

def increase_timeout(timeout=3):
    def wrapped_top(fn):
        def wrapped(*args, **kwargs):
            caller = args[0]
            prev_timeout = caller.get_timeout()
            caller.set_timeout(timeout)
            (status, ret) = fn(*args, **kwargs)
            caller.set_timeout(prev_timeout)
            return (status, ret)
        return wrapped
    return wrapped_top

def trace(fn):
    """
    Displays function name and argument list of the decorated function

    """
    def wrapped(*args, **kwargs):
        caller = args[0]
        if(caller.get_trace()):
            func_text = '  ' +  fn.__name__ + '('
            for arg in range(1, len(args)):
                func_text += format(args[arg])
                if (len(args) > 2 and arg < (len(args) - 1)):
                    func_text += ','
            func_text += ')  '

            # extract opcodes from functions docstring text
            re1='.*?'           # Non-greedy match on filler
            re2='(Main:)'       # Literal match
            re3='.*?'           # Non-greedy match on filler
            re4='((?:[a-z][a-z]*[0-9]+[a-z0-9]*))'  # opcode as text
            re5='.*?'           # Non-greedy match on filler
            re6='(Secondary:)'  # Literal match
            re7='.*?'           # Non-greedy match on filler
            re8='((?:[a-z][a-z]*[0-9]+[a-z0-9]*))' # opcode as text
            rg = re.compile(re1+re2+re3+re4+re5+re6+re7+re8,re.IGNORECASE|re.DOTALL)
            m = rg.search(fn.__doc__)
            if m:
                op1=m.group(2)
                op2=m.group(4)
                opcodes = 'M:0{} S:0{}'.format(op1, op2)
            else:
                opcodes = ''

            trace_text = '{:<45}{}'.format(func_text, opcodes)
            cprint (trace_text, 'green')

        result = fn(*args, **kwargs)
        return result
    wrapped.__name = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


class API(object):
    """
    PySummit system control functions common to both Master and Slaves

    """

    def __init__(self, target, name):
        self.target = target
        self.name = name
        self.IO_FUNC = ctypes.CFUNCTYPE(ctypes.c_ubyte, ctypes.POINTER(ms.MESSAGE))
        self.ACCESS_FUNC = ctypes.CFUNCTYPE(ctypes.c_ubyte)
        self._retries = 5
        self._trace = False
        self._log_errors_only = False


    @property
    def log_errors_only(self):
        """Property used to determine if all status values should be logged or
        just error status"""
        return self._log_errors_only

    @log_errors_only.setter
    def log_errors_only(self, value):
        if not isinstance(value, bool):
            raise TypeError("log_errors_only property must be True or False")
        self._log_errors_only = value

    def decode_error_status(self, status, cmd=None, print_on_error=False):
        ret = ""
        if status != 0x01:
            if cmd:
                ret += "%s -- %s (0x%.2X)" % (cmd, self.status_codes.get(status, 'Unknown Error'), status)
            else:
                ret += "-- %s (0x%.2X)" % (self.status_codes.get(status, 'Unknown Error'), status)

            if print_on_error:
                cprint (ret, 'red')
#                print ret
            else:
                return ret

    def get_trace(self):
        """
        Returns state of decorated API call trace display mode

        | Arguments: none
        |
        | Returns:
        |  enable -- current state of trace mode
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  state = Tx.get_trace()
        |  print state
        """
        return self._trace

    def set_trace(self, enable):
        """
        Controls state of decorated API call trace display mode

        | Arguments:
        |   enable -- True or False
        |
        | Returns: none
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  Tx.set_trace(True)
        """
        self._trace = enable

    def get_retries(self):
        """
        Returns current number of retries used by PySummit API methods

        | Arguments: none
        |
        | Returns:
        |  retries -- number of retries
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  value = Tx.get_retries()
        |  print "Retries = ", value
        """
        return self._retries

    def set_retries(self, retries):
        """
        Sets number of retries used by PySummit API methods

        | Arguments:
        |  retries -- number of retries
        |
        | Returns: none
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  Tx.set_retries(8)
        """
        if(retries >= 0):
            self._retries = retries

    def open(self, wr_func, rd_func, open_func, close_func):
        """
        Set com port function pointers for interface type

        | Arguments:
        |  wr_func    -- interface specific com write function
        |  rd_func    -- interface specific com read function
        |  open_func  -- interface specific com open function
        |  close_func -- interface specific com close function
        |
        | Returns: None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  Tx.open(wr, rd, opn, cls)
        """

        self.target.SWM_Open(wr_func, rd_func, open_func, close_func)

    def close(self):
        """
        Closes communication port connection with device

        | Arguments: none
        |
        | Returns: None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  Tx.close()
        """

        self.target.SWM_Close()

#==============================================================================
# API Methods
#==============================================================================
    @trace
    @retry_datalog
    def rd(self, addr):
        """
        Reads specified Olympus asic register

        | Arguments:
        |  addr -- asic register address
        |
        | Returns:
        |  status -- system status code
        |  value  -- register data
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.rd(0x400018)
        |  print "Status = ", status, "Register data = ", value
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Register Access command
        |  Olympus Register Specification Document
        """

        reg = ctypes.c_ushort()
#        status = self.target.DiagDriverGetRegister(addr, ctypes.byref(reg))
        status = self.target.SWM_Diag_GetRegister(addr, ctypes.byref(reg))
        return (status, reg.value)

    @trace
    @retry_datalog
    def wr(self, addr, data):
        """
        Writes specified Olympus asic register

        | Arguments:
        |  addr -- asic register address
        |  data -- value to be written to specified register
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.wr(0x400018, 0x5555)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Register Access command
        |  Olympus Register Specification Document
        """

        status = self.target.SWM_Diag_SetRegister(addr, data)
        return (status, None)

    @trace
    @retry_datalog
    def set_transmit_power(self, power):
        """
        Sets the transmit power level, expressed in dBm.

        | Arguments:
        |  power  -- transmit power level (dBm)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.set_transmit_power(15)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x17
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Transmit Power command
        """

        status = self.target.SWM_Diag_SetTransmitPower(power)
        return (status, None)

    @trace
    @datalog
    def get_transmit_power(self):
        """
        Returns current transmit power level

        | Arguments: none
        |
        | Returns:
        |  status  -- system status code
        |  power  -- current transmit power level
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, power) = Tx.get_transmit_power()
        |  print "Status = ", status, "Transmit Power = ", power
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x17
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Transmit Power command
        """

        power = ctypes.c_ubyte()
        status = self.target.SWM_Diag_GetTransmitPower(ctypes.byref(power))
        return (status, power.value)

    @trace
    @retry_datalog
    def set_radio_channel(self, radio, channel):
        """
        Sets operating radio channel of specified radio

        | Arguments: none
        |  radio   -- radio (0 = main, 1 = monitor)
        |  channel -- desired radio channel
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.set_radio_channel(0, 8)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x06
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Radio Channel command
        """

        status = self.target.SWM_Diag_SetRadioChannel(radio, channel)
        return (status, None)

    @trace
    @datalog
    def get_radio_channel(self):
        """
        Returns current radio channel of master main radio

        | Arguments: none
        |
        | Returns:
        |  status  -- system status code
        |  channel -- current radio channel
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, channel) = Tx.get_radio_channel()
        |  print "Status = ", status, "Radio channel = ", channel
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x06
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Radio Channel command
        """

        channel = ctypes.c_ushort()
        status = self.target.SWM_Diag_GetRadioChannel(ctypes.byref(channel))
        return (status, channel.value)

    @trace
    @increase_timeout(20)
    @datalog
    def transmit_packets(self, num_packets):
        """
        Allows main radio to transmit specified number of packets

        | Arguments:
        |  num_packets -- number of packet to transmit (<= 50000)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  status = Tx.transmit_packets(100)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x08
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Transmit Packet command
        """

        MAX_PACKETS = 50000  # loosely based on HW timeouts
        assert isinstance(num_packets, int)
        if not (num_packets <= MAX_PACKETS):
            cprint("Number of packets must be <= %d" % MAX_PACKETS, 'red')
            return

        packet_count = ctypes.c_uint32(num_packets)
        status = self.target.SWM_Diag_Tx(ctypes.byref(packet_count))
        return (status, None)

    @trace
    @datalog
    def receive_statistics(self):
        """
        Returns network receive quality statistics and last packet received

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  stats  -- receive statistics struct defined by typedef DIAGNOSTIC_RECEIVE_STATISTICS
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  stats = DIAGNOSTIC_RECEIVE_STATISTICS()
        |  (status, stats) = Tx.receive_statistics()
        |  print "Status = ", status, "Packet count = ", stats.totalPacketCount
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x09
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Receive Packet command
        """

        stats_struct = desc.DIAGNOSTIC_RECEIVE_STATISTICS()
        status = self.target.SWM_Diag_RxGetLastPacket(ctypes.byref(stats_struct))
        return (status, stats_struct)

    @trace
    @datalog
    def reset_rx_statistics(self):
        """
        Resets accumulated network receive quality statistics

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  (status, null) = Rx.reset_rx_statistics()
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x09
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Receive Packet command
        """

        status = self.target.SWM_Diag_RxReset()
        return (status, None)

    @trace
    @datalog
    def get_system_quality(self):
        """
        Returns the master system quality

        | Arguments: none
        |
        | Returns:
        |  status  -- system status code
        |  quality -- master system quality
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, quality) = Tx.get_system_quality()
        |  print "Status = ", status, "System quality = ", quality
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0E
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Get System Quality command
        """

        quality = ctypes.c_ushort()
        status = self.target.SWM_Diag_GetSystemQuality(ctypes.byref(quality))
        return (status, quality.value)


## FWUpdate Commands
    @trace
    @retry_datalog
    def get_active_image(self, slave):
        """
        Returns current active image index for specified device

        | Arguments:
        |  slave -- device index (0 to 10 for slaves, 0xFE for master)
        |
        | Returns:
        |  status -- system status code
        |  active_image -- firmware image index (0 or 1)
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, image) = Tx.get_active_image(0xFE)
        |  print "Status = ", status, "Active image = ", active_image
        |
        | Opcodes:
        |  Main: 0x40, Secondary: 0x01
        |
        | See also:
        |  Summit SWM908 API Specification, Firmware Update Messages, Firmware Active Image Index command
        """

        active_image = ctypes.c_ubyte()
        status = self.target.SWM_FWUpdate_GetActiveImage(slave, ctypes.byref(active_image))
        return (status, active_image.value)

    @trace
    @increase_timeout(3)
    @retry_datalog
    def set_active_image(self, slave, active_image):
        """
        Sets active image index for specified device

        | Arguments:
        |  slave -- device index (0 to 10 for slaves, 0xFE for master)
        |  active_image -- firmware image index (0 or 1)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, image) = Tx.get_active_image(0xFE)
        |  print "Status = ", status, "Active image = ", active_image
        |
        | Opcodes:
        |  Main: 0x40, Secondary: 0x01
        |
        | See also:
        |  Summit SWM908 API Specification, Firmware Update Messages, Firmware Active Image Index command
        """

        status = self.target.SWM_FWUpdate_SetActiveImage(slave, active_image)
        return (status, None)

    @trace
    @increase_timeout(3)
    @retry_datalog
    def erase_fw_image(self, slave, image):
        """
        Erases firmware image for specified device and image index

        | Arguments:
        |  slave        -- device index (0 to 10 for slaves, 0xFE for master)
        |  active_image -- firmware image index (0 or 1)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.erase_fw_image(0xFE, 0)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x40, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Firmware Update Messages, Firmware Erase Image command
        """

        status = self.target.SWM_FWUpdate_EraseImage(slave, image)
        return (status, None)

    @trace
    @increase_timeout(3)
    @retry_datalog
    def load_firmware(self, slave, image, relative_address, length, flashData):
        """
        Loads segment of firmware image into buffer

        | Arguments:
        |  slave            -- device index (0 to 10 for slaves, 0xFE for master)
        |  image            -- firmware image index (0 or 1)
        |  relative_address -- offset into firmware buffer to write data
        |  length           -- number of bytes to load
        |  flashData        -- buffer containing firmware image
        |
        | Returns:
        |  status -- system status code
        |  value  -- number of bytes written to firmware buffer
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  with open('Apollo_0186_Release.nvm', 'rb') as f:
        |    flashData = array.array('B', f.read(0x80))
        |  cnt = len(flashData)
        |  (status, value) = Tx.load_firmware(0, 0, 0, cnt, flashData)
        |  print "Status = ", status, "Bytes loaded = ", value
        |
        | Opcodes:
        |  Main: 0x40, Secondary: 0x02
        |
        | See also:
        |  load_fw_from_file()
        |  Summit SWM908 API Specification, Firmware Update Messages, Firmware Load Image command
        """

        cnt = ctypes.c_uint32()
        cnt.value = length
        buffer = (ctypes.c_ubyte * len(flashData)).from_buffer(flashData)
        status = self.target.SWM_FWUpdate_LoadFirmware(slave, image, relative_address, ctypes.byref(cnt), buffer)
        return (status, cnt.value)

    @trace
    @increase_timeout(3)
    @retry_datalog
    def check_active_image(self, slave, active_image):
        """
        Performs CRC check on firmware image to verify correctness

        | Arguments:
        |  slave        -- device index (0 to 10 for slaves, 0xFE for master)
        |  active_image -- firmware image index (0 or 1)
        |
        | Returns:
        |  status -- system status code
        |  value  -- active image status (1 = verification passed, 0xFF = failed)
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.check_active_image(0xFE, 0)
        |  print "Status = ", status, "Verification = ", value
        |
        | Opcodes:
        |  Main: 0x40, Secondary: 0x03
        |
        | See also:
        |  Summit SWM908 API Specification, Firmware Update Messages, Firmware Verify Image command
        """

        image_ok = ctypes.c_ubyte()
        status = self.target.SWM_FWUpdate_CheckActiveImage(slave, active_image, ctypes.byref(image_ok))
        return (status, image_ok.value)

    @trace
    @increase_timeout(30)
    @datalog
    def load_fw_to_eeprom(self, start_address=0x00):
        """
        Load a firmware image to an I2C driven EEPROM.

        | Arguments:
        |  start_address -- the starting address of the EEProm
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.load_fw_to_eeprom(1, 0)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x40, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Firmware Update Messages, Firmware Verify Image command
        """
        assert isinstance(start_address, int)

        (status, active_image) = self.get_active_image(0xFE)
        assert active_image in [0, 1]

        if(status == 1):
            image_number = ctypes.c_ubyte(active_image)
            start_address = ctypes.c_uint32(start_address)
            status = self.target.SWM_FWUpdate_CopyFirmwareTo(image_number, start_address)

        return (status, None)

    @trace
    @increase_timeout(20)
    @datalog
    def load_fw_from_eeprom(self, start_address=0x00):
        """
        Update module firmware from an external I2C EEPROM.

        | Arguments:
        |  start_address -- the starting address of the EEProm
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.update_fw_from_eeprom(1, 0)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x40, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Firmware Update Messages, Firmware Verify Image command
        """
        assert isinstance(start_address, int)

        (status, active_image) = self.get_active_image(0xFE)
        assert active_image in [0, 1]

        if status == 1:
            if(active_image == 0):
                image = 1
            elif(active_image == 1):
                image = 0

            image = ctypes.c_ubyte(image)
            start_address = ctypes.c_uint32(start_address)

            # It takes ~2.5 seconds to erase flash on first pass, so increase
            # timeout. blurg
            (status, null) = self.erase_fw_image(0xFE, image)

            # erase returns right away on a master. Add a delay to compensate
            if(self['type'] == "master"):
                time.sleep(3)

            if(status not in [0x01, 0x02]):
                print "Firmware Image %d could not be erased (0x%.2X)" % (image, status)
                return (status, None)
            status = self.target.SWM_FWUpdate_CopyFirmwareFrom(image, start_address)

            if status == 1:
                (status, image_ok) = self.check_active_image(0xFE, image)
                self.decode_error_status(status, 'set_check_active_image', print_on_error=True)
                if((status == 0x01) and (image_ok == 1)):
                    (status, null) = self.set_active_image(0xFE, image)
                else:
                    print "Active image didn't check out: 0x%.2X" % (image_ok)

        return (status, None)

    @trace
    def get_time_info(self):
        """
        Returns syslog time information

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  data   -- syslog time info struct as defined by typedef SYSLOG_TIMEINFO
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  data = desc.SYSLOG_TIMEINFO()
        |  (status, data) = Tx.get_time_info()
        |  print "Status = ", status, "Uptime = ", data.uptime
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x11
        |
        | See also:
        |  This is an undocumented command for Summit internal use
        """

        data = desc.SYSLOG_TIMEINFO()
        status = self.target.SWM_Diag_GetSysLogTimeInfo(ctypes.byref(data))
        return (status, data)

    @trace
    def get_syslog_data(self):
        """
        Returns syslog data

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  [data, entries] -- syslog data struct as defined by typedef SYSLOG_ENTRY, number of entries
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.get_syslog_data()
        |  print "Status = ", status, "Entries = ", value.entries
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x10
        |
        | See also:
        |  This is an undocumented command for Summit internal use
        """

        bytes  = ctypes.c_uint16()
        data = desc.SYSLOG_ENTRIES()
        status = self.target.SWM_Diag_GetSysLogData(ctypes.byref(bytes), ctypes.byref(data))
        entries = bytes.value/ctypes.sizeof(desc.SYSLOG_ENTRY)
        return (status, (data, entries))

    @trace
    @retry_datalog
    def mfg_dump(self, file):
        """
        Reads device manufacturing data from flash and writes it to specified file

        | Arguments:
        |  file -- file name to be written with manufacturing data
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.mfg_dump('mfg_data.mfg')
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Access command
        """

        parameters = ctypes.c_char_p("%s %s" % (self['type'], file))
        status = self.target.DiagCommandMfgDump(parameters)
        return(status, None)

    @trace
    @increase_timeout(3)
    @retry_datalog
    def mfg_load(self, file):
        """
        Reads manufacturing data from specified file and writes it to device flash

        | Arguments:
        |  file -- file name containing data to be written to device
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.mfg_load('mfg_data.mfg')
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Access command
        """

        parameters = ctypes.c_char_p("%s %s" % (self['type'], file))
        status = self.target.DiagCommandMfgLoad(parameters)
        return(status, None)

    @trace
    @retry_datalog
    def temperature(self):
        """
        Reads temperature of device asic

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  temp   -- asic temperature as degrees C
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, temp) = Tx.temperature()
        |  print "Status = ", status, "Temperature = ", temp, "C"
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0B
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Temperature Query command
        """

        temp = ctypes.c_ushort()
        status = self.target.SWM_Diag_GetTemperature(ctypes.byref(temp))
        return (status, temp.value)

    @increase_timeout(20)
    def get_pdout(self, delay, sample_count):
        """
        Returns a value that represents the power detect output voltage level
        from the Airoha radio.

        | Agruments:
        | delay -- number of 80MHz clock cycles to wait before sampling.
        | sample_count -- number transmissions to be sent and averaged
        |
        | Returns:
        | status -- system status code
        | pdout -- power detect out value
        |
        | Example:
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  (status, pdout) = Rx.get_pdout(9000, 32)
        |  print "Status = ", status
        |  print "PD out = 0x%X" % pdout
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x13
        |
        | See also:
        |  This is currently a reserved command.
        """
        pdout = ctypes.c_ushort()
        status = self.target.SWM_Diag_GetPdout(delay, sample_count, ctypes.byref(pdout))
        return (status, pdout.value)

    def set_power_comp_enable(self, enable):
        """
        Enable or disable power compensation.

        | Arguments
        | enable -- 1=enable, 0=disable
        |
        | Returns:
        | status -- system status code
        | value -- None
        |
        | Example:
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  (status, null) = Rx.set_power_comp_enable(1)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x07
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Thermal Compensation Command
        """
        status = self.target.SWM_Diag_SetPowerCompEnable(enable)
        return (status, None)

    @trace
    @retry_datalog
    def wrr(self, radio, addr, data):
        """
        Writes a Radio (Airoha) register

        | Arguments:
        |  radio -- radio select (0 for Working, else Monitor)
        |  addr -- radio register address (0x0 to 0xF)
        |  data -- 20-bit (hex) value to be written
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  # Write 0x01E05 to the Working radio's RX gain reg (0xA)
        |  (status, null) = Tx.wrr(0, 0xA, 0x01E05)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x19
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Register Access command
        |  Olympus Register Specification Document
        """
        status = self.target.SWM_Diag_SetRadioRegister(radio, addr, data)
        return (status, None)

    @trace
    @retry_datalog
    def get_duty_cycle(self):
        """
        Returns the duty cycle of command packets generated by the
        'tx' command. Values returned by a TX module assume operation
        at 18MB/s and values returned by an RX assume operation at 6MB/s.
        The value returned is a percentage expressed as an integer.

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  duty_cycle -- the 'tx' packet duty cycle
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, duty_cycle) = Tx.get_duty_cycle()
        |  print "Status = ", status, "Duty Cycle= ", duty_cycle
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x1A
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Temperature Query command
        """

        duty_cycle = ctypes.c_ubyte()
        status = self.target.SWM_Diag_GetTxDutyCycle(ctypes.byref(duty_cycle))
        return (status, duty_cycle.value)

#==============================================================================
# API Convenience Methods
#==============================================================================
    @trace
    def get_devid(self):
        """
        Reads device ID from asic DEVID register

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  devid  -- device ID
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.get_devid()
        |  print "Status = ", status, "Device ID = ", devid
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Register Access command
        |  Olympus Register Specification Document
        """

        (status, devid) = self.rd(0x400008)
        return (status, devid)

    @trace
    def get_tx_antenna(self):
        """
        Returns which Tx antenna is selected from the asic RF_PWR_CNTL register

        | Arguments: none
        |
        | Returns:
        |  status  -- system status code
        |  antenna -- antenna selected (0 to 3)
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, antenna) = Tx.get_tx_antenna()
        |  print "Status = ", status, "Antenna = ", antenna
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Register Access command
        |  Olympus Register Specification Document
        """

        (status, antenna) = self.rd(0x401018)
        return (status, ((antenna>>6) & 3))

    @trace
    def get_rx_antenna(self):
        """
        Returns which Rx antenna is selected from the asic RF_PWR_CNTL register

        | Arguments: none
        |
        | Returns:
        |  status  -- system status code
        |  antenna -- antenna selected (0 to 3)
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, antenna) = Tx.get_rx_antenna()
        |  print "Status = ", status, "Antenna = ", antenna
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Register Access command
        |  Olympus Register Specification Document
        """

        (status, antenna) = self.rd(0x401018)
        return (status, ((antenna>>4) & 3))

    @trace
    def get_our_mac(self):
        """
        Returns MAC address read from the asic OURMAC registers

        | Arguments: none
        |
        | Returns:
        |  status  -- system status code
        |  mac     -- current OURMAC address read from asic
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, mac) = Tx.get_our_mac()
        |  print "Status = ", mac, "MAC = ", mac
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Register Access command
        |  Olympus Register Specification Document
        """

        (st0, our_mac0) = self.rd(0x403024)
        (st1, our_mac1) = self.rd(0x403028)
        (st2, our_mac2) = self.rd(0x40302c)
        mac = "%.2X:%.2X:%.2X:%.2X:%.2X:%.2X" % (our_mac0 & 0x00ff, (our_mac0 & 0xff00) >> 8,
                                                 our_mac1 & 0x00ff, (our_mac1 & 0xff00) >> 8,
                                                 our_mac2 & 0x00ff, (our_mac2 & 0xff00) >> 8,
                                                 )
        status = st0 & st1 & st2
        return (status, mac)

    @trace
    def get_src_mac(self):
        """
        Returns MAC address read from the asic SRCMAC registers

        | Arguments: none
        |
        | Returns:
        |  status  -- system status code
        |  mac     -- current SRCMAC address read from asic
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, mac) = Tx.get_src_mac()
        |  print "Status = ", status, "MAC = ", mac
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Register Access command
        |  Olympus Register Specification Document
        """

        (st0, src_mac0) = self.rd(0x403018)
        (st1, src_mac1) = self.rd(0x40301c)
        (st2, src_mac2) = self.rd(0x403020)
        mac = "%.2X:%.2X:%.2X:%.2X:%.2X:%.2X" % (src_mac0 & 0x00ff, (src_mac0 & 0xff00) >> 8,
                                                 src_mac1 & 0x00ff, (src_mac1 & 0xff00) >> 8,
                                                 src_mac2 & 0x00ff, (src_mac2 & 0xff00) >> 8,
                                                 )
        status = st0 & st1 & st2
        return (status, mac)

    @trace
    def put_src_mac(self, src_mac):
        """
        Loads MAC address into the asic SRCMAC registers

        | Arguments:
        |  mac -- MAC address as: "02:EA:00:00:00:01"
        |
        | Returns:
        |  status  -- system status code
        |  value   -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.set_src_mac("02:EA:3C:00:04:3F")
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Register Access command
        |  Olympus Register Specification Document
        """

        src_mac = src_mac.split(':')
        w0 = (int(src_mac[1],16) << 8) + int(src_mac[0],16)
        w1 = (int(src_mac[3],16) << 8) + int(src_mac[2],16)
        w2 = (int(src_mac[5],16) << 8) + int(src_mac[4],16)

        (st0, null) = self.wr(0x403018, w0)
        (st1, null) = self.wr(0x40301c, w1)
        (st2, null) = self.wr(0x403020, w2)
        status = st0 & st1 & st2
        return (status, None)

    @trace
    def scanning(self):
        """
        Returns slave RF scanning state from Olympus asic GPIO_OUT register

        | Arguments: none
        |
        | Returns:
        |  status   -- system status code
        |  scanning -- (0 = not scanning, 1 = scanning)
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, scanning) = Tx.scanning()
        |  print "Status = ", status, "Scanning state = ", scanning
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Register Access command
        |  Olympus Register Specification Document
        """
        (status, gpio_out) = self.rd(0x4000b0)
        scan = (gpio_out & 0x01)
        return (status, int(not scan))

    @trace
    def load_fw_from_file(self, filename, slave=0xFE):
        """
        Erases inactive flash image, writes .nvm file to flash, verifies it and sets as active image

        | Arguments:
        |  filename -- file name of firmware iamge to be loaded
        |  slave    -- device index (0 to 10 for slaves, 0xFE for master)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.load_fw_from_file('Apollo_0191_Release.nvm', 0x1)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x40, Secondary: 0x02
        |
        | See also:
        |  load_firmware()
        |  Summit SWM908 API Specification, Firmware Update Messages, Firmware Load Image command
        """

        self.logger.debug("get_active_image(%d)" % slave)
        (status, active_image) = self.get_active_image(slave)
        self.logger.debug("active_image: %d" % active_image)
        if(status != 1):
            return (status, None)

        if(active_image == 0):
            image = 1
        elif(active_image == 1):
            image = 0
        else:
            self.logger.debug("invalid image")
            return (-1, None)

        address = 0
        success = True
        file_size = os.path.getsize(filename)
        with open(filename, 'rb') as f:
            # It takes ~2.5 seconds to erase flash on first pass, so increase
            # timeout. blurg
            self.logger.debug("erase_fw_image(%d, %d)" % (slave, image))
            (status, null) = self.erase_fw_image(slave, image)

            # erase returns right away on a master. Add a delay to compensate
            if(self['type'] == "master"):
                time.sleep(3)

            if(status not in [0x01, 0x02]):
                print "Firmware Image %d could not be erased (0x%.2X)" % (image, status)
                return (status, None)

            total_byte_count = 0
            while(total_byte_count < file_size):
                flashData = array.array('B', f.read(FLASH_BUFFER_LENGTH))
                cnt = len(flashData)
                total_byte_count += cnt

                attempts = 15
                for attempt in range(1, attempts+1, 1):
                    self.logger.debug("load_firmware()")
                    (status, bytes_transferred) = self.load_firmware(slave, image, address, cnt, flashData)
                    if(status != 1):
                        self.logger.debug(self.decode_error_status(status, 'load_firmware'))
#                        return (status, None)

                    if(bytes_transferred > 0):
                        break
                    elif(attempt == attempts):
                        self.logger.error('\nMax attempts exceeded')
                        print "Bytes to send: %d" % file_size
                        print "Bytes sent: %d" % total_byte_count
                        print "Attempt: %d" % attempt
                        return (status, None)

                address += bytes_transferred
                if(address != 0 and (address % (FLASH_BUFFER_LENGTH * 4)) == 0):
                    sys.stdout.write('.')
                    sys.stdout.flush()

            sys.stdout.write('\n')
            sys.stdout.flush()

            (status, image_ok) = self.check_active_image(slave, image)
            if((status == 0x01) and (image_ok == 1)):
                (status, null) = self.set_active_image(slave, image)
            else:
                print "Active image didn't check out: %s" % (self.decode_error_status(status))
        return (status, None)

#==============================================================================
#                if cnt != 0:
#                    attempts = 5
#                    for attempt in range(attempts):
#                        self.logger.debug("load_firmware attempt %d" % (attempt))
#                        self.logger.debug("load_firmware(%d, %d, 0x%X, %d" % (slave, image, address, cnt))
#                        (status, bytes_transferred) = self.load_firmware(slave, image, address, cnt, flashData)
#                        self.logger.debug("bytes_transferred: %d" % (bytes_transferred))
#                        if(status != 1):
#                            success = False
#                            self.logger.debug(self.decode_error_status(status, 'load_firmware'))
#                            break
#
#                        elif ((bytes_transferred == 0) and (1+attempt == attempts)):
#                            print "total bytes so far: %d" % total_byte_count
#                            status = -1
#                            success = False
#                            break
#
#                    if((address % (FLASH_BUFFER_LENGTH * 4)) == 0):
#                        sys.stdout.write('.')
#                        sys.stdout.flush()
#                    address += bytes_transferred
#                else:
#                    sys.stdout.write('\n')
#                    sys.stdout.flush()
#                    break
#        if(success):
#            (status, image_ok) = self.check_active_image(slave, image)
#            if((status == 0x01) and (image_ok == 1)):
#                (status, null) = self.set_active_image(slave, image)
#            else:
#                print "Active image didn't check out: %s" % (dec.decode_status())
#        return (status, None)

    @trace
    def get_flash_data(self, addr, num_bytes):
        assert isinstance(addr, int)
        assert isinstance(num_bytes, int)
        return_buffer = list()
        c_ubyte_array = (ctypes.c_ubyte * num_bytes)
        c_buffer = c_ubyte_array()
        status = self.target.SWM_Diag_GetFlashData(addr,
                                                    num_bytes,
                                                    ctypes.byref(c_buffer))
        for i in range(int(num_bytes)):
            return_buffer.append(c_buffer[i])
        return (status, return_buffer)

    @trace
    def erase_flash(self):
        """
        Erases slave's entire flash memory

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  (status, null) = Rx.erase_flash()
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x03
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Erase command
        """

        status = self.target.SWM_Diag_EraseFlash()
        return (status, None)

    @trace
    @retry_datalog
    def mfg_read_file(self, file):
        """
        Read specified MFG text file to internal data structure

        | Arguments:
        |  file -- file name containing data to be written to device
        |
        | Returns:
        |  status -- system status code
        |  value  -- manufacturing datastructure
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, tx_mfg_data_struct) = Tx.mfg_read_file('mfg_data.txt')
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Access command
        """

        filename = ctypes.c_char_p("%s" % (file))
        if self['type'] == 'master':
            mfg_ds = desc.FLASH_MASTER_MFG_DATA_SECTION()
            status = self.target.DiagCommandMfgLoadTxStructure(filename,
                ctypes.byref(mfg_ds))
        elif self['type'] == 'slave':
            mfg_ds = desc.DATAFLASH_SPEAKER_MFG_DATA_SECTION()
            status = self.target.DiagCommandMfgLoadRxStructure(filename,
                ctypes.byref(mfg_ds))
        else:
            raise Exception("unknown device type. Not 'master' or 'slave'")

        return(status, mfg_ds)

    @trace
    @retry_datalog
    def mfg_write_file(self, file, mfg_ds):
        """
        Write specified MFG data structure to text file.

        | Arguments:
        |  file -- file name containing data to be written to device
        |  mfg_ds -- manufacturing data structure
        |            TX: FLASH_MASTER_MFG_DATA_SECTION
        |            RX: DATAFLASH_SPEAKER_MFG_DATA_SECTION
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.mfg_write_file('mfg_data.txt', mfg_ds)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Access command
        """

        filename = ctypes.c_char_p("%s" % (file))
        if self['type'] == 'master':
            status = self.target.DiagCommandMfgDumpTxStructure(filename,
                ctypes.byref(mfg_ds))
        elif self['type'] == 'slave':
            status = self.target.DiagCommandMfgDumpRxStructure(filename,
                ctypes.byref(mfg_ds))
        else:
            raise Exception("unknown device type. Not 'master' or 'slave'")

        return(status, None)

    @trace
    @retry_datalog
    def get_mfg_data(self):
        """
        Get MFG data from flash and return the appropriate data structure

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  mfg_ds -- manufacturing data structure
        |            TX: FLASH_MASTER_MFG_DATA_SECTION
        |            RX: DATAFLASH_SPEAKER_MFG_DATA_SECTION
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, tx_mfg_ds) = Tx.get_mfg_data()
        |  print "Status = ", status
        """
        if self['type'] == 'master':
            mfg_ds = desc.FLASH_MASTER_MFG_DATA_SECTION()
        elif self['type'] == 'slave':
            mfg_ds = desc.DATAFLASH_SPEAKER_MFG_DATA_SECTION()
        else:
            raise Exception("unknown device type. Not 'master' or 'slave'")

        status = self.target.SWM_Diag_GetFlashData(0x0c0000, ctypes.sizeof(mfg_ds), ctypes.byref(mfg_ds))
        return (status, mfg_ds)

    @trace
    @increase_timeout(3)
    @retry_datalog
    def set_mfg_data(self, mfg_ds):
        """
        Takes an MFG data structure and writes it to flash.

        | Arguments: mfg_ds
        |
        | Returns
        |  status -- system status code
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.set_mfg_data(mfg_ds)
        |  print "Status = ", status
        """
        if self['type'] == 'master':
            assert ctypes.sizeof(mfg_ds) == ctypes.sizeof(desc.FLASH_MASTER_MFG_DATA_SECTION)
        elif self['type'] == 'slave':
            assert ctypes.sizeof(mfg_ds) == ctypes.sizeof(desc.DATAFLASH_SPEAKER_MFG_DATA_SECTION)
        else:
            raise Exception("unknown device type. Not 'master' or 'slave'")

        status = self.target.SWM_Diag_EraseFlashSector(0x0c)
        time.sleep(3) # It takes up to 3 seconds for flash to erase
        if(status == 0x01):
            status = self.target.SWM_Diag_SetFlashData(0x0c0000, ctypes.sizeof(mfg_ds), ctypes.byref(mfg_ds))

        return (status, None)

#==============================================================================
# Chime Methods
#==============================================================================
    @trace
    @retry_datalog
    def chime_rx(self, tone, volume):
        """
        Play a tone or white noise to an individual RX device.

        | Arguments:
        |  tone     -- the frequency of tone (0xFF to turn off)
        |  volume   -- volume of tone
        |
        | Returns
        |  status -- system status code
        |  value  -- none
        |
        | Valid tone values:
        |   0:  24 Hz
        |   1:  48 Hz
        |   2:  96 Hz
        |   3:  120 Hz
        |   4:  192 Hz
        |   5:  240 Hz
        |   6:  384 Hz
        |   7:  480 Hz
        |   8:  600 Hz
        |   9:  960 Hz
        |   10: 1200 Hz
        |   11: 1920 Hz
        |   12: 2400 Hz
        |   13: 3000 Hz
        |   14: 4800 Hz
        |   15: 6000 Hz
        |   16: 9600 Hz
        |   17: 12000 Hz
        |   18: 24000 Hz
        |   19: White Noise
        |
        | Example:
        |
        |  from pysummit.devices import RxAPI
        |  Rx = TxAPI()
        |  (status, value) = Rx.chime_rx(19, 0x13000)  // on
        |  print status
        |  (status, value) = Rx.chime_rx(0xFF, 0x13000)  // off
        |  print status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x15
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Zone Commands
        """
        assert isinstance(tone, int)
        assert isinstance(volume, int)
        status = 0x02  #  Invalid command
#        attrs = vars(self)
#        print attrs

#       if (re.search('Master', self.name)):
#           status = self.target.SWM_Network_Chime(slave_id, tone, duration)
        if (re.search('Slave', self.name)):
            status = self.target.SWM_Diag_Chime(tone, volume)
        else:
            print("Illegal target: %s" % self.target)

        return (status, None)

    @trace
    @retry_datalog
    def chime(self, slave_id, tone, duration):
        """
        Play a tone or white noise to an individual RX device.

        | Arguments:
        |  slave_id -- slave index
        |  tone     -- the frequency of the tone
        |  duration -- duration of the tone (mSec.)
        |
        | Returns
        |  status -- system status code
        |  value  -- none
        |
        | Valid tone values:
        |   0:  24 Hz
        |   1:  48 Hz
        |   2:  96 Hz
        |   3:  120 Hz
        |   4:  192 Hz
        |   5:  240 Hz
        |   6:  384 Hz
        |   7:  480 Hz
        |   8:  600 Hz
        |   9:  960 Hz
        |   10: 1200 Hz
        |   11: 1920 Hz
        |   12: 2400 Hz
        |   13: 3000 Hz
        |   14: 4800 Hz
        |   15: 6000 Hz
        |   16: 9600 Hz
        |   17: 12000 Hz
        |   18: 24000 Hz
        |   19: White Noise
        |
        | Example:
        |
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.chime(0, 19, 3000)
        |  print status
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x19
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Zone Commands
        """
        assert isinstance(slave_id, int)
        assert isinstance(tone, int)
        assert isinstance(duration, int)
        status = 0x02  #  Invalid command
#        attrs = vars(self)
#        print attrs

        if (re.search('Master', self.name)):
            status = self.target.SWM_Network_Chime(slave_id, tone, duration)
#       elif (re.search('Slave', self.name)):
#           status = self.target.SWM_Diag_Chime(slave_id, tone, duration)
        else:
            print("Illegal target: %s" % self.target)

        return (status, None)


class TxAPI(API):
    """
    PySummit system functions specific to control of Master device

    """

    def __init__(self, name='Master', collect=True, com='i2c', param1=True, param2=True, bsp=None):
        # Setup function pointers
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.getLogger().level)
        lib_filename = resource_filename(__name__,"SWMTXAPI.so")
        self.logger.debug("lib_filename: %s" % lib_filename)
#        lib_filename = resource_filename(Requirement.parse("pysummit"),"SWMTXAPI.so")
        super(TxAPI, self).__init__(ctypes.CDLL(lib_filename), name)
        self.bsp = bsp
        self.__dev = {
            'com': None,
            'com_type': None,
            'port': None,
            'fw_major': "0.0",
            'fw_minor': "0.0",
            'fw_version': "0.0",
            'mac': None,
            'type': 'master',
            'zone': 0,
            'vendor_id': None,
            'product_id': None,
            }

        # Create dictionary with both Summit status
        self.status_codes = {}
        self.status_codes.update(dec.system_status_tx)

        if isinstance(com, comport.ComPort):
#            print "Connect to TX device via UART port %s" % com.target.port
            self.__dev['com'] = com
            self.__dev['port'] = com.target.port
            self.__dev['com_type'] = 'UART'
            self.status_codes.update(dec.serial_status)
            print "Using UART port {}".format(self.__dev['port'])
            self.open_func = self.ACCESS_FUNC(self._py_uart_open_func)
            self.logger.debug("open_func: %r" % self.open_func)

            self.close_func = self.ACCESS_FUNC(self._py_uart_close_func)
            self.logger.debug("close_func: %r" % self.close_func)

            self.wr_func = self.IO_FUNC(self._py_uart_wr_func)
            self.logger.debug("wr_func: %r" % self.wr_func)

            self.rd_func = self.IO_FUNC(self._py_uart_rd_func)
            self.logger.debug("rd_func: %r" % self.rd_func)
        elif com == 'usb':
            self.__dev['com_type'] = 'USB'
            self.status_codes.update(dec.usb_status)

            # look for optional vendor and product ID arguments
            if param1 == None and param2 == None:
                self.__dev['vendor_id'] = 0x2495
                self.__dev['product_id'] = 0x0016
                print "Using USB"
            elif param1 != None and param2 != None:
                self.__dev['vendor_id'] = int(param1, 0)
                self.__dev['product_id'] = int(param2, 0)
                print "Using USB <VendorID:0x%04x, ProductID:0x%04x>" \
                  % (self.__dev['vendor_id'], self.__dev['product_id'])
            else:
                print '\n<<< Using USB but optional vendor and product IDs missing or incorrect >>>\n'
                raise Exception()

            self.open_func = self.ACCESS_FUNC(self._py_usb_open_func)
            self.logger.debug("open_func: %r" % self.open_func)

            self.close_func = self.ACCESS_FUNC(self._py_usb_close_func)
            self.logger.debug("close_func: %r" % self.close_func)

            self.wr_func = self.IO_FUNC(self._py_usb_wr_func)
            self.logger.debug("wr_func: %r" % self.wr_func)

            self.rd_func = self.IO_FUNC(self._py_usb_rd_func)
            self.logger.debug("rd_func: %r" % self.rd_func)
        elif com == 'i2c':
            self.__dev['com_type'] = 'I2C'
            self.status_codes.update(dec.i2c_status)
            print "Using I2C"
            self.open_func = self.ACCESS_FUNC(self.bsp.target.I2C_Open)
            self.logger.debug("open_func: %r" % self.open_func)

            self.close_func = self.ACCESS_FUNC(self.bsp.target.I2C_Close)
            self.logger.debug("close_func: %r" % self.close_func)

            self.wr_func = self.IO_FUNC(self.bsp.target.I2C_Write)
            self.logger.debug("wr_func: %r" % self.wr_func)

            self.rd_func = self.IO_FUNC(self.bsp.target.I2C_Read)
            self.logger.debug("rd_func: %r" % self.rd_func)

        else:
            print 'No Tx Control interface specified'
            raise

        self.open(collect)

    def __getitem__(self, index):
        return self.__dev[index]

    def __setitem__(self, index, value):
        self.__dev[index] = value

    @trace
    def open(self, collect=True):
        """
        Opens Raspberry Pi I2C communication with master and retrieves master descriptor data

        | Arguments: none
        |
        | Returns: None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  Tx.open()
        """
        super(TxAPI, self).open(self.wr_func, self.rd_func, self.open_func, self.close_func)
        if collect:
            self.collect_master_info()

    @trace
    def collect_master_info(self):
        """Gets various setup information from the master"""

        # (re) discover usb device
        if self.__dev['com_type'] == 'USB':
            self.__dev['com'] = usb.core.find(idVendor=self.__dev['vendor_id'],\
            idProduct=self.__dev['product_id'])

        (status, md) = self.get_master_descriptor()
        if(status == 0x01):
            mod = md.moduleDescriptor
            major = mod.firmwareVersion >> 5   # (Upper 11-bits)
            minor = mod.firmwareVersion & 0x1f # (Lower 5-bits)
            self['fw_major'] = major
            self['fw_minor'] = minor
            self['fw_version'] = "%d.%d" % (major, minor)
            self['mac'] = ":".join(["%.2X" % i for i in mod.macAddress])
            self.__dev['module_id'] = mod.moduleID
        else:
            print '<<< No TX device detected on %s interface >>>' \
            % self.__dev['com_type']

        (status, zone) = self.get_speaker_zone()
        if(status != 0x01):
            print self.decode_error_status(status, "get_speaker_zone()")
        else:
            self['zone'] = zone

    @trace
    def id(self):
        """
        Returns master id

        | Arguments: none
        |
        | Returns:
        |  id -- (0 for master)
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  value = Tx.id()
        |  print value
        """

        return 0

    @trace
    def get_timeout(self):
        """
        Returns current command timeout value

        | Arguments: none
        |
        | Returns:
        |  timeout -- command timeout value
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  value = Tx.get_timeout()
        |  print value
        """
        if self['com_type'] == 'UART':
            return self['com'].target.timeout

    @trace
    def set_timeout(self, timeout):
        """
        Sets command timeout value

        | Arguments:
        |  timeout -- command timeout value in seconds
        |
        | Returns: none
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  Tx.set_timeout(3)
        """
        if self['com_type'] == 'UART':
            assert isinstance(timeout, int)
            self['com'].target.timeout = timeout

    @trace
    @retry_datalog
    def dfs_dump(self, file):
        """
        Read device DFS engine parameters from flash and write to specified file

        | Arguments:
        |  file -- file name to be written with manufacturing data
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.dfs_dump('dfs_data.txt')
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Access command
        """

        parameters = ctypes.c_char_p("%s" % (file))
        status = self.target.DFS_DriverDumpDFSParameters(parameters)
        return(status, None)

    @trace
    @retry_datalog
    def dfs_load(self, file):
        """
        Load specified DFS engine parameter file to device flash

        | Arguments:
        |  file -- file name containing data to be written to device
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.dfs_load('dfs_data.txt')
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Access command
        """

        parameters = ctypes.c_char_p("%s" % (file))
        status = self.target.DFS_DriverLoadDFSParameters(parameters)
        return(status, None)

    @trace
    @retry_datalog
    def dfs_read_file(self, file):
        """
        Read specified DFS engine parameter file to internal data structure

        | Arguments:
        |  file -- file name containing data to be written to device
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.dfs_read_file('dfs_data.txt')
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Access command
        """

        parameters = ctypes.c_char_p("%s" % (file))
        radio_channel_section = desc.RADIO_CHANNEL_SECTION()
        status = self.target.DFS_DriverLoadDFSParametersStructure(parameters, ctypes.byref(radio_channel_section))
        return(status, radio_channel_section)


#==============================================================================
# USB Callback functions
#==============================================================================
    def _py_usb_wr_func(self, mes):
        """
        Private method supporting write communication via USB interface
        """
        try:
            status = 0xE6  # was E1
            message = mes[0].to_pkt()
            bytes_written = self.__dev['com'].ctrl_transfer(0x22, 0x03, 0, 0,  message, 1000)
            if(len(message) == bytes_written):
                status = 0
        except:
            pass
        return status

    def _py_usb_rd_func(self, mes):
        """
        Private method supporting read communication via USB interface
        """
        try:
            status = 0xE7  # was E1
            message = mes[0].to_pkt()
            bytes_written = self.__dev['com'].ctrl_transfer(0x22, 0x03, 0, 0, message, 1000)
            if(len(message) == bytes_written):
                resp = self.__dev['com'].ctrl_transfer(0xa2, 0x03, 0, 0, 500, 10000)
                try:
                    status = 0xE8 # was E3
                    mes[0].from_pkt(resp.tostring())
                except TargetPacketError as info:
                    pass
                except:
                    raise
            status = 0
        except:
            pass
        return status

    def _py_usb_open_func(self):
        """
        Private method supporting opening communication via USB interface
        """
        return 0

    def _py_usb_close_func(self):
        """
        Private method supporting closing communication via USB interface
        """
        return 0

    def _py_reset_func(self):
        """
        Private method supporting reset when using USB interface
        """
        return 0

#==============================================================================
# UART Callback functions
#==============================================================================
    def _py_uart_wr_func(self, mes):
        status = 0x0
        self['com'].lock_port()
        try:
            if(self['com'].isOpen()):
                message = mes[0].to_pkt()
                bytes_written = self['com'].write(message)
                if(len(message) != bytes_written):
                    status = 0xE1
                else:
                    status = 0
            else:
                print "self['com'] is *NOT* open"
        except:
            raise
        finally:
            self['com'].unlock_port()

        return status

    def _py_uart_rd_func(self, mes):
        """Serial read method that searches for correct Summit protocol 1 byte
        at a time.

        """
        self['com'].lock_port()
        self['com'].target.flushInput()
        message = mes[0].to_pkt()

        bytes_written = self['com'].write(message)
        if(bytes_written == 0):
            self['com'].unlock_port()
            return 0xE1
        else:
            status = 0

        timeout_counter = 0
        byte_count = 0
        while(True): # Read until exception or return
            timeout_counter += 1
            if timeout_counter > 500:
                return 0xE5

            byte = self['com'].read(1)  # Tries to read until timeout
            if(len(byte) != 1):
                return 0xE1

            if(byte_count == 0):
                if(ord(byte) == 0x01):
                    message = byte
                    byte_count += 1
            elif(byte_count > 0):
                if((ord(byte) == 0x01) & (ord(message[0]) == 0x01)):
                    message += byte
                    byte_count += 1
                    message += self['com'].read(7)
                    if(len(message) != 9):
                        return 0xE2
                    else:
                        data_len = ord(message[7]) + (ord(message[8])<<8)
                        message += self['com'].read(data_len)

                        try:
                            mes[0].from_pkt(message)
                        except TargetPacketError as info:
                            self['com'].unlock_port()
                            return 0xE4
                        except:
                            raise
                        if(len(message) != (data_len+9)):
                            self['com'].unlock_port()
                            return 0xE3 # READ_PAYLOAD_ERROR

                        self['com'].unlock_port()
                        return status

    def _py_uart_open_func(self):
        return self.__dev['com'].connect()

    def _py_uart_close_func(self):
        return self['com'].close()

    def _py_uart_reset_func(self):
        return 0

#==============================================================================
# API Methods
#==============================================================================
    @trace
    @datalog
    def dfs_channel_select(self, monitor_index, working_index):
        """
        Selects static working and monitor radio channels (disabling DFS engine automatic channel changes)

        | Arguments:
        |  monitor_index -- monitor radio channel (0 to 34)
        |  working_index -- working radio channel (0 to 34)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.dfs_channel_select(7, 3)
        |  print status
        |
        | Opcodes:
        |  Main: 0x30, Secondary: 0x01
        |
        | See also:
        |  Summit SWM908 API Specification, DFS Messages, Channel Select Override command
        """

        status = self.target.SWM_DFS_SetStaticChannels(
            monitor_index, working_index)
        return (status, None)

    @trace
    @datalog
    def dfs_get_engine_state(self):
        """
        Retrieves current DFS engine state information

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  state  -- DFS Engine state struct defined by typedef DFS_ENGINE_STATUS
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  state = DFS_ENGINE_STATUS()
        |  (status, state) = Tx.dfs_get_engine_state()
        |  print "Status = ", status, "Working channel = ", state.workingChannelIndex
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, DFS Engine State Query command
        """

        state = desc.DFS_ENGINE_STATUS()
        status = self.target.SWM_DFS_GetEngineState(ctypes.byref(state))
        return (status, state)

    @trace
    @datalog
    def dfs_override(self, enable=0):
        """
        Disables DFS automatic channel switching

        | Arguments:
        |  enable -- selects mode (0 = normal, 1 = disable)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.dfs_override(1)
        |  print status
        |
        | Opcodes:
        |  Main: 0x30, Secondary: 0x02
        |
        | See also:
        |  Summit SWM908 API Specification, DFS Messages, Channel Timeout Override command
        """
        status = self.target.SWM_DFS_SetDFSOverride(enable)
        return (status, None)

    @trace
    @retry_datalog
    def set_tpm_mode(self, mode):
        """
        Sets the TPM user mode.

        | Arguments:
        |  mode  --  Disable (0), Max Channels (1), Max Distance (2)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.set_tpm_mode(1)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x30, Secondary: 0x03
        |
        | See also:
        |  Summit SWM908 API Specification, DFS Messages, TPM User Mode
        """

        status = self.target.SWM_DFS_SetTpmMode(mode)
        return (status, None)

    @trace
    @datalog
    def get_tpm_mode(self):
        """
        Returns the current TPM User Mode.

        | Arguments: none
        |
        | Returns:
        |  status  -- system status code
        |  mode  --  Disable (0), Max Channels (1), Max Distance (2)
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, mode) = Tx.get_tpm_mode()
        |  print "Status = ", status, "TPM User Mode = ", mode
        |
        | Opcodes:
        |  Main: 0x30, Secondary: 0x03
        |
        | See also:
        |  Summit SWM908 API Specification, DFS Messages, TPM User Mode
        """

        mode = ctypes.c_ubyte()
        status = self.target.SWM_DFS_GetTpmMode(ctypes.byref(mode))
        return (status, mode.value)

    @trace
    @datalog
    def get_tpm_attributes(self):
        """
        Retrieves the current TPM Region Attributes

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  tpm_attributes -- TPM Attributes struct
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, tpm_attributes) = Tx.get_tpm_attributes()
        |
        | Opcodes:
        |  Main: 0x30, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, DFS Messages, DFS TPM Attributes Query command
        """

        tpm_attributes = desc.DFS_TPM_ATTRIBUTES()
        status = self.target.SWM_DFS_GetTpmAttributes(ctypes.byref(tpm_attributes))
        return (status, tpm_attributes)

    @trace
    @retry_datalog
    def slave_count(self):
        """
        Returns number of slaves currently enumeratered in system

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  count  -- number of slaves currently enumerated
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, count) = Tx.slave_count()
        |  print "Status = ", status, "Slaves = ", count
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x03
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Speaker Count Query command
        """

        count = ctypes.c_ubyte()
        status = self.target.SWM_Master_GetSpeakerCount(ctypes.byref(count))
        return (status, count.value)

    @trace
    @datalog
    def set_i2s_clocks(self, audio_clock_setup):
        """
        Specifies the master's I2S interface configuration

        | Arguments:
        |  audio_clock_setup -- configuration data struct defined by typedef AUDIO_CLOCK_SETUP
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  clks = desc.AUDIO_CLOCK_SETUP()
        |  clks.audioSource = 0x1
        |  clks.audioSetup.sclkFrequency = 0x2
        |  clks.audioSetup.driveClks = 0x1
        |  clks.audioSetup.mclkFrequency = 0x2
        |  clks.audioSetup.mclkOutputEnable = 0x1
        |  (status, value) = Tx.set_i2s_clocks(clks)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x09
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Audio Port Initialization command
        """

        status = self.target.SWM_Master_SetupAudioClock(audio_clock_setup)
        return (status, None)

    @trace
    @datalog
    def set_i2s_input_map(self, i2s_input_map):
        """
        Maps speakers to I2S channels per map struct data

        | Arguments:
        |  i2s_input_map -- speaker to I2S channel map defined by typedef SPEAKER_TYPE_TO_I2S_MAP
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  I2S_MAP = desc.SPEAKER_TYPE_TO_I2S_MAP * 11
        |  chan_map = I2S_MAP()
        |  chan_map[0].codecI2SChannel = 0
        |  chan_map[0].codecChannel = 1
        |  chan_map[0].speakerType = 2
        |  (status, value) = Tx.set_i2s_input_map(chan_map)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x0E
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Speaker Type to I2S Channel Association command
        """

        status = self.target.SWM_Master_SetI2SInputMap(ctypes.byref(i2s_input_map))
        return (status, None)

    @trace
#    @datalog
    def push_map(self, slave_index, speaker_map_info, size=1):
        """
        Load speaker map structure into specified slave

        | Arguments:
        |  slave_index      -- slave to load with map
        |  speaker_map_info -- speaker location struct defined by typedef SPEAKER_MAP_INFO
        |  size             -- how many SPEAKER_MAP_INFOs to pass at one time
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  map = desc.SPEAKER_MAP_INFO()
        |  map.speakerVectorDistance = 960
        |  map.speakerType = 0x2
        |  (status, null) = Tx.push_map(2, map)
        |  print status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Speaker Location command
        """

        if (1 == size):
            status = self.target.SWM_Master_SetSpeakerMapInfo(
                slave_index,
                ctypes.byref(speaker_map_info))
        else: status = self.target.SWM_Master_SetMultiSpeakerMapInfo(
                slave_index,
                ctypes.byref(speaker_map_info),
                size)
        return (status, None)

    @trace
    @datalog
    def get_map_type(self):
        """
        Retrieves the speaker map currently suppported by the Summit system

        | Arguments: none
        |
        | Returns:
        |  status   -- system status code
        |  map_type -- map type (0x1 to 0x1C, as defined in SWM908 API)
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, map_type) = Tx.get_map_type()
        |  print "Status = ", status, "Map type = ", map_type
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x08
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Map Type Query command
        """

        map_type = ctypes.c_ubyte()
        status = self.target.SWM_Master_GetMapType(ctypes.byref(map_type))
        return (status, map_type.value)

    @trace
    @datalog
    def keep(self, enable):
        """
        Selects operational state of speaker keeper, the automatic network quality controller

        | Arguments:
        |  enable -- (0 = disable, 1 = enable)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.keep(1)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x0C
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, SpeakerKeeper command
        """

        status = self.target.SWM_Master_SpeakerKeeper(enable)
        return (status, None)

    @trace
    @datalog
    def setRxMAC(self, index, mac):
        """
        Sets specified slave MAC address, temporarily overriding value stored in flash

        | Arguments:
        |  index -- slave id (0 to 10, 0xFF to select all)
        |  mac   -- MAC address specified as typedef
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  def myint(x): return int(x, 16)
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  mac = "02:EA:3C:00:04:3F"
        |  macaddress = map(myint, mac.split(':'))
        |  (status, null) = Tx.setRxMAC('1', macaddress)
        |  print status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x12
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Set RX MAC command
        """

        buffer = (ctypes.c_ubyte * len(mac))(*mac)
        status = self.target.SWM_Diag_SetRxMAC(int(index,0), buffer)
        return (status, None)

    @trace
    @datalog
    def beacon(self, time, channel):
        """
        Master broadcasts beacon signal to begin network enumeration process

        | Arguments:
        |  time    -- beacon transmit time as milliseconds
        |  channel -- radio channel (default = 99)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.beacon(3000, 99)
        |  print status
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x01
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Beacon command
        """

        assert isinstance(time, int)
        assert isinstance(channel, int)
        if (time > desc.BUSY_TIMEOUT):
            cprint("Period must be <= %d" % desc.BUSY_TIMEOUT, 'red')
            return

        status = self.target.SWM_Network_Beacon(time, channel)
        return (status, None)

    @trace
    @datalog
    def discover(self, dis_type):
        """
        Master broadcasts discovery command causing speakers to respond with configuration data

        | Arguments:
        |  dis_type -- discovery type (0 for fast discovery, 1 for full discovery)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.discover(0x1)
        |  print status
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x02
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Discovery command
        """

        status = self.target.SWM_Network_Discovery(dis_type)
        return (status, None)

    @trace
    @datalog
    def reset(self, slave_index):
        """
        Releases connection between master and specified slave returning it to associate mode

        | Arguments:
        |  slave_index -- speaker id (0 to 10, 0xFF for all)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.reset(0)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Reset command
        """

        status = self.target.SWM_Network_Reset(slave_index)
        return (status, None)

    @datalog
    def gpio_reset(self):
        """
        Asserts Raspberry Pi GPIB bit to reset master

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.gpio_reset()
        """
        status = self.bsp.dut_reset()
        if self.__dev['com_type'] == 'USB':
            for i in range(5): # trying to re-discover device
                self.__dev['com'] = usb.core.find(idVendor=self.__dev['vendor_id'],\
                idProduct=self.__dev['product_id'])
                if self.__dev['com'] != None:
                    break

        return (status, None)

    @datalog
    def gpio_button_press(self, duration):
        """
        Asserts Raspberry Pi GPIO bit to emulate pressing USB module button

        | Arguments: duration -- time (in seconds) to assert button press
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.gpio_button_press(3.0)
        |  print status
        """
        status = self.bsp.button_press(duration)
        return (status, None)


    @trace
    def reboot(self):
        """
        Reboots the master as if the reset button was pressed

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.reboot()
        |  print status
        """

        return self.gpio_reset()

    @trace
    @retry_datalog
    def coef(self, slave_id, table_id):
        """
        Selects the speaker coefficient table used by specified slave

        | Arguments:
        |  slave_id -- speaker id (0 to 10 or 0xFF for all)
        |  table_id -- coeffcient table select (0 to 9)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.coef(0xFF, 0)
        |  print status
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x0A
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Coefficient Select command
        """

        status = self.target.SWM_Network_SelectCoefTable(slave_id, table_id)
        return (status, None)

    @trace
    @retry_datalog
    def delay(self, slave_id, delay):
        """
        Sets bulk delay for an RX device or all connected RX devices.

        | Arguments:
        |  slave_id     -- speaker id (0 to 10 or 0xFF for all)
        |  delay (usec) -- coeffcient table select (16 bits))
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.delay(0xFF, 100)
        |  print status
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x0B
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Audio Delay Override command
        """
        status = self.target.SWM_Network_AssignAudioDelay(slave_id, delay)
        return (status, None)


    @trace
    @retry_datalog
    def restore(self):
        """
        Causes master to restore previously saved speaker configuration data

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.restore()
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x10
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Restore Speaker Configurations command
        """
        status = self.target.SWM_Master_RestoreSystem()
        return (status, None)

    @trace
    @datalog
    def shutdown(self):
        """
        Shuts down system and puts slave amplifiers into power saving state

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.shutdown()
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x0B
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, System Shutdown command
        """

        status = self.target.SWM_Master_ShutDown()
        return (status, None)

    @trace
    @datalog
    def start(self):
        """
        Updates speaker parameters and changes network state from idle to isochronous operation

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.start()
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x0C
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Network Run command
        """

        status = self.target.SWM_Network_Run()
        return (status, None)

    @trace
    @datalog  # Halt
    def stop(self):
        """
        Mutes system and changes network state from isochronous to idle

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.stop()
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x0D
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Network Halt command
        """

        status = self.target.SWM_Network_Shutdown()
        return (status, None)

    @trace
    @datalog
    def slot(self, slave_index, slot):
        """
        Assigns audio slot (source channel) to specified speaker

        | Arguments:
        |  slave_index -- speaker id (0 to 10, 0xFF for all)
        |  slot        -- audio slot (1 to 8, 0 to disable)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.slot(0, 3)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x03
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Slot Assignment command
        """

        status = self.target.SWM_Network_AssignSlot(slave_index, slot)
        return (status, None)

    @trace
    @datalog
    def mute(self, mute=1):
        """
        Mutes entire system (sets speaker volume to 0 or restores previous volume)

        | Arguments:
        |  mute -- mute state (0 = unmute, 1 = mute)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.mute(1)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Mute command
        """
        status = self.target.SWM_Network_SetMute(mute)
        return (status, None)


    @trace
    @retry_datalog
    def get_mute(self):
        """
        Retrieve mute from master

        | Arguments:
        |  none
        |
        | Returns:
        |  status -- system status code
        |  mute  -- mute value
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, buffer) = Tx.get_mute()
        |  print "Status = %d", status
        |  print "Mute = %d", mute
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Access command
        """
        mute = ctypes.c_ubyte(0)

        status = self.target.SWM_Network_GetMute(ctypes.byref(mute))
        return (status, mute.value)


    @trace
    @trace
    @datalog
    def volume(self, volume_table, volume_value):
        """
        Sets system volume via broadcast to all enumerated speakers

        | Arguments:
        |  volume_table -- selects volume table (0 for linear, 1 for log)
        |  volume_value -- sets level (linear table: 0 to 1048575, log table: 0 to 1175)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.volume(0, 300000)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x0E
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Volume command
        """

#        volume_info = desc.VOLUME_INFO()
#        volume_info.tableID = volume_table
#        volume_info.volume = volume_value
#        status = self.target.SWM_Network_SetVolume(volume_info)
        status = self.target.SWM_Network_SetVolume(volume_table, volume_value)
        return (status, None)

    @trace
    @retry_datalog
    def get_volume(self):
        """
        Read volume from master into internal volume structure

        | Arguments:
        |  none
        |
        | Returns:
        |  status -- system status code
        |  volume_info  -- volume info struct
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, buffer) = Tx.get_volume()
        |  print "Status = ", status
        |  print "Volume = ", buffer.volume
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Access command
        """
        volume = desc.VOLUME_INFO()
        status = self.target.SWM_Network_GetVolume(ctypes.byref(volume))
        return (status, volume)


    @trace
    @datalog
    def echo(self, slave_index, retry=1):
        """
        Master sends echo command to specified speaker, speaker responds with acknowledge

        | Arguments:
        |  slave_index -- speaker id (0 to 10)
        |  retry       -- number of times to retry echo attempt
        |
        | Returns:
        |  status     -- system status code
        |  rx_antenna -- echo return value
        |  tx_antenna -- echo return value
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, [rx_antenna, tx_antenna]) = Tx.echo(0, 5)
        |  print "Status = ", status, "Rx antenna = ", rx_antenna, "Tx antenna = ", tx_antenna
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x06
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Echo command
        """

        tx_antenna = ctypes.c_ubyte()
        rx_antenna = ctypes.c_ubyte()
        status = self.target.SWM_Network_SpeakerEcho(
            slave_index,
            retry,
            ctypes.byref(rx_antenna),
            ctypes.byref(tx_antenna))
        return (status, [rx_antenna.value, tx_antenna.value])

    @trace
    @datalog
    def change_radio_channel(self, radio, channel):
        """
        Selects the operating channel of the specified radio

        | Arguments:
        |  radio   -- 0 for main, 1 for radar detect
        |  channel -- radio channel (default = 99)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.change_radio_channel(0, 99)
        |  print status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x06
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Radio Channel command
        """

        status = self.target.SWM_Diag_SetRadioChannel(radio, channel)
        return (status, None)

    @trace
    @retry_datalog
    def get_master_operating_state(self, speaker_index=0):
        """
        Retrieves current master operational state information

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  buffer -- Master operating state struct defined by typedef MASTER_OPERATING_STATE
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.MASTER_OPERATING_STATE()
        |  (status, buffer) = Tx.get_master_operating_state()
        |  print "Status = ", status, "System mode = ", buffer.systemMode
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x02
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Master Operational State Query command
        """

        assert isinstance(speaker_index, int)
        type = 0
        buffer = desc.MASTER_OPERATING_STATE()
        status = self.target.SWM_Master_GetMasterDescriptorInfo(type, speaker_index, ctypes.byref(buffer))
        return (status, buffer)

### Not implemented
#    @trace
#    @retry_datalog
#    def set_master_operating_state(self, buffer):
#        """
#        Sets current master operational state information
#
#        | Arguments:
#        |  buffer -- Master operating state struct defined by typedef MASTER_OPERATING_STATE
#        |
#        | Returns:
#        |  status -- system status code
#        |
#        | Example:
#        |  import descriptors as desc
#        |  from pysummit.devices import TxAPI
#        |  Tx = TxAPI()
#        |  buffer = desc.MASTER_OPERATING_STATE()  ## edit fields
#        |  (status) = Tx.set_master_operating_state(buffer)
#        |  print "Status = ", status
#        |
#        | Opcodes:
#        |  Main: 0x10, Secondary: 0x02
#        |
#        | See also:
#        |  Summit SWM908 API Specification, Master Messages, Master Operational State Query command
#        """
#
#        assert isinstance(speaker_index, int)
#        assert isinstance(buffer, desc.MASTER_OPERATING_STATE)
#        type = 0
#        status = self.target.SWM_Master_SetMasterDescriptorInfo(type,
#            speaker_index,
#            ctypes.byref(buffer),
#            ctypes.sizeof(buffer))
#        return (status, None)

    @trace
    @retry_datalog
    def get_master_descriptor(self):
        """
        Retrieves master descriptor information contained in master flash memory

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  buffer -- master descriptor information struct defined by typedef MASTER_DESCRIPTOR
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.MASTER_DESCRIPTOR()
        |  (status, buffer) = Tx.get_master_descriptor()
        |  print "Status = ", status, "MAC address = ", ":".join(["%.2X" % i for i in buffer.moduleDescriptor.macAddress])
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x02
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Master Descriptor Query command
        """

        type = 1
        speaker_index = 0
        buffer = desc.MASTER_DESCRIPTOR()
        status = self.target.SWM_Master_GetMasterDescriptorInfo(type, speaker_index, ctypes.byref(buffer))
        return (status, buffer)

    @trace
#    @retry_datalog
    def set_master_descriptor(self, buffer):
        """
        Sends master descriptor information to master memory

        | Arguments:
        |  buffer -- master descriptor information struct defined by typedef MASTER_DESCRIPTOR
        |
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.MASTER_DESCRIPTOR()  ## edit fields
        |  (status) = Tx.set_master_descriptor(buffer)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x02
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Master Descriptor Query command
        """
        assert isinstance(buffer, desc.MASTER_DESCRIPTOR)
        type = 1
        speaker_index = 0
        status = self.target.SWM_Master_SetMasterDescriptorInfo(type,
            speaker_index,
            ctypes.byref(buffer),
            ctypes.sizeof(buffer))
        return (status, None)

    @trace
    @retry_datalog
    def get_master_speaker_descriptor(self, speaker_index=0):
        """
        Retrieves speaker descriptor information contained in master flash memory

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  buffer -- speaker descriptor information struct defined by typedef SPEAKER_DESCRIPTOR
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.SPEAKER_DESCRIPTOR()
        |  (status, buffer) = Tx.get_master_speaker_descriptor()
        |  print "Status = ", status, "Amplifier s/n = ", buffer.amplifierDescriptor.serialNumber
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x02
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Master Speaker Descriptor Query command
        """

        assert isinstance(speaker_index, int)
        type = 2
        buffer = desc.SPEAKER_DESCRIPTOR()
        status = self.target.SWM_Master_GetMasterDescriptorInfo(type, speaker_index, ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def set_master_speaker_descriptor(self, speaker_index, buffer):
        """
        Sets speaker descriptor information to master memory

        | Arguments:
        |  speaker_index -- speaker descriptor index (0 - 2)
        |  buffer -- speaker descriptor information struct defined by typedef SPEAKER_DESCRIPTOR
        |
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.SPEAKER_DESCRIPTOR()  ## edit fields
        |  (status) = Tx.set_master_speaker_descriptor(0, buffer)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x02
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Master Speaker Descriptor Query command
        """

        assert isinstance(speaker_index, int)
        assert isinstance(buffer, desc.SPEAKER_DESCRIPTOR)
        type = 2
        status = self.target.SWM_Master_SetMasterDescriptorInfo(type,
            speaker_index,
            ctypes.byref(buffer),
            (ctypes.sizeof(buffer) - ctypes.sizeof(desc.AMPLIFIER_DESCRIPTOR)))
        return (status, None)

    @trace
    @retry_datalog
    def get_master_wisa_descriptor(self):
        """
        Retrieves WISA descriptor information contained in master flash memory

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  buffer -- WISA descriptor information struct defined by typedef WISA_DESCRIPTOR
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.WISA_DESCRIPTOR()
        |  (status, buffer) = Tx.get_master_wisa_descriptor()
        |  print "Status = ", status, "WISA version = ", buffer.wisaVersion
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x02
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Wisa Descriptor Query command
        """

        type = 3
        speaker_index = 0
        buffer = desc.WISA_DESCRIPTOR()
        status = self.target.SWM_Master_GetMasterDescriptorInfo(type, speaker_index, ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def set_master_wisa_descriptor(self, buffer):
        """
        Sends WISA descriptor information to master memory

        | Arguments:
        |  buffer -- WISA descriptor information struct defined by typedef WISA_DESCRIPTOR
        |
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.WISA_DESCRIPTOR()  ## edit field
        |  (status) = Tx.set_master_wisa_descriptor(buffer)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x02
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Wisa Descriptor Query command
        """

        assert isinstance(buffer, desc.WISA_DESCRIPTOR)
        type = 3
        speaker_index = 0
        buffer = desc.WISA_DESCRIPTOR()
        status = self.target.SWM_Master_SetMasterDescriptorInfo(type,
            speaker_index,
            ctypes.byref(buffer),
            ctypes.sizeof(buffer))
        return (status, None)

    @trace
    @retry_datalog
    def save_master_mfg_data(self):
        """
        Saves MFG data into flash

        | Arguments: None
        |
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status) = Tx.save_mfg_data()
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x01
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Master Status command
        """
        status = self.target.SWM_Master_SaveMfgData()
        return (status, None)

    @trace
    @retry_datalog
    def get_master_key_status(self):
        """
        Retrieves key master descriptor information contained in master flash memory

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  buffer -- Key master descriptor information struct defined by typedef MASTER_KEY_STATUS
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.MASTER_KEY_STATUS()
        |  (status, buffer) = Tx.get_master_key_status()
        |  print "Status = ", status, "HW type = ", buffer.hardwareType
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x02
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Master Descriptor
        """

        type = 8
        speaker_index = 0
        buffer = desc.MASTER_KEY_STATUS()
        status = self.target.SWM_Master_GetMasterDescriptorInfo(type, speaker_index, ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def get_master_speaker_location_descriptor(self, slave_index=0):
        """
        Retrieves speaker location descriptor information contained in master flash memory

        | Arguments:
        |  slave_index -- speaker_id (0 to 10)
        |
        | Returns:
        |  status -- system status code
        |  buffer -- speaker location struct defined by typedef SPEAKER_MAP_INFO
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.SPEAKER_MAP_INFO()
        |  (status, buffer) = Tx.get_master_speaker_location_descriptor(0)
        |  print "Status = ", status, "Speaker x distance to listener = ", buffer.speakerX
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Speaker Location command
        """

        buffer = desc.SPEAKER_MAP_INFO()
        status = self.target.SWM_Master_GetSpeakerMapInfo(
            slave_index,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def get_speaker_operating_state(self, slave_index=0, read_from_network=0):
        """
        Retrieves specified speaker's operational status data

        | Arguments:
        |  slave_index -- speaker_id (0 to 10)
        |  read_from_network -- (0 = read from slave, 1 = read from master)
        |
        | Returns:
        |  status -- system status code
        |  buffer -- speaker descriptor struct defined by typedef SPEAKER_OPERATING_STATE
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.SPEAKER_OPERATING_STATE()
        |  (status, buffer) = Tx.get_speaker_operating_state(1, 0)
        |  print "Status = ", status, "Mode = ", buffer.slaveMode
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Speaker Information Query command
        """
        request_type = 0
        speaker_descriptor_index = 0
        buffer = desc.SPEAKER_OPERATING_STATE()
        status = self.target.SWM_Network_SpeakerInfo(slave_index,
            request_type,
            speaker_descriptor_index,
            read_from_network,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def get_speaker_module_descriptor(self, slave_index=0, read_from_network=0):
        """
        Retrieves specified speaker module's descriptor data from flash

        | Arguments:
        |  slave_index -- speaker_id (0 to 10)
        |  read_from_network -- (0 = read from slave, 1 = read from master)
        |
        | Returns:
        |  status -- system status code
        |  buffer -- speaker descriptor struct defined by typedef MODULE_DESCRIPTOR
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.MODULE_DESCRIPTOR()
        |  (status, buffer) = Tx.get_speaker_module_descriptor(0, 0)
        |  print "Status = ", status, "MAC address = ", ":".join(["%.2X" % i for i in buffer.macAddress])
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """

        request_type = 1
        speaker_descriptor_index = 0
        buffer = desc.MODULE_DESCRIPTOR()
        status = self.target.SWM_Network_SpeakerInfo(slave_index,
            request_type,
            speaker_descriptor_index,
            read_from_network,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def get_speaker_descriptor(self, slave_index=0, read_from_network=0, speaker_descriptor_index=0):
        """
        Retrieves specified speaker's descriptor 0 data from flash

        | Arguments:
        |  slave_index -- speaker_id (0 to 10)
        |  read_from_network -- (0 = read from slave, 1 = read from master)
        |
        | Returns:
        |  status -- system status code
        |  buffer -- speaker descriptor struct defined by typedef SPEAKER_DESCRIPTOR
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.SPEAKER_DESCRIPTOR()
        |  (status, buffer) = Tx.get_speaker_descriptor(0, 0)
        |  print "Status = ", status, "s/n = ", buffer.serialNumber
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """

        request_type = 2
        buffer = desc.SPEAKER_DESCRIPTOR()
        status = self.target.SWM_Network_SpeakerInfo(slave_index,
            request_type,
            speaker_descriptor_index,
            read_from_network,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def get_speaker_wisa_descriptor(self, slave_index=0, read_from_network=0):
        """
        Retrieves specified speaker's WISA descriptor data from flash

        | Arguments:
        |  slave_index -- speaker_id (0 to 10)
        |  read_from_network -- (0 = read from slave, 1 = read from master)
        |
        | Returns:
        |  status -- system status code
        |  buffer -- speaker descriptor struct defined by typedef WISA_DESCRIPTOR
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.WISA_DESCRIPTOR()
        |  (status, buffer) = Tx.get_speaker_wisa_descriptor(0, 0)
        |  print "Status = ", status, "Wisa version = ", buffer.wisaVerson
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Speaker Information Query command
        """

        request_type = 3
        speaker_descriptor_index = 0
        buffer = desc.WISA_DESCRIPTOR()
        status = self.target.SWM_Network_SpeakerInfo(slave_index,
            request_type,
            speaker_descriptor_index,
            read_from_network,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def get_speaker_key_status(self, slave_index=0, read_from_network=0):
        """
        Retrieves specified speaker's key status descriptor data from flash

        | Arguments:
        |  slave_index -- speaker_id (0 to 10)
        |  read_from_network -- (0 = read from slave, 1 = read from master)
        |
        | Returns:
        |  status -- system status code
        |  buffer -- speaker descriptor struct defined by typedef SPEAKER_KEY_STATUS
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.SPEAKER_KEY_STATUS()
        |  (status, buffer) = Tx.get_speaker_key_status(0, 0)
        |  print "Status = ", status, "Key status = ", buffer.firmwareVersion
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x05
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Speaker Information Query command
        """

        request_type = 8
        speaker_descriptor_index = 0
        buffer = desc.SPEAKER_KEY_STATUS()
        status = self.target.SWM_Network_SpeakerInfo(slave_index,
            request_type,
            speaker_descriptor_index,
            read_from_network,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @trace
    @retry_datalog
    def get_speaker_global_coefficient_data_descriptor(self, slave_index=0, read_from_network=0):
        """
        Retrieves the specified speaker's global coefficient data

        | Arguments:
        |  slave_index -- speaker_id (0 to 10)
        |  read_from_network -- (0 = read from slave, 1 = read from master)
        |
        | Returns:
        |  status -- system status code
        |  buffer -- speaker coeffcient struct defined by typedef GLOBAL_COEFFICIENT_INFO
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.GLOBAL_COEFFICIENT_INFO()
        |  (status, buffer) = Tx.get_speaker_global_coefficient_data_descriptor(0, 0)
        |  print "Status = ", status, "Max volume index = ", buffer.speakerMaxVolumeIndex
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """

        request_type = 5
        speaker_descriptor_index = 0
        buffer = desc.GLOBAL_COEFFICIENT_INFO()
        status = self.target.SWM_Network_SpeakerInfo(slave_index,
            request_type,
            speaker_descriptor_index,
            read_from_network,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def get_speaker_current_coefficient_data_descriptor(self, slave_index=0, read_from_network=0):
        """
        Retrieves the specified speaker's currently selected coefficient data

        | Arguments:
        |  slave_index -- speaker_id (0 to 10)
        |  read_from_network -- (0 = read from slave, 1 = read from master)
        |
        | Returns:
        |  status -- system status code
        |  buffer -- speaker coeffcient struct defined by typedef CURRENT_COEFFICIENT_INFO
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.CURRENT_COEFFICIENT_INFO()
        |  (status, buffer) = Tx.get_speaker_current_coefficient_data_descriptor(0, 0)
        |  print "Status = ", status, "Max volume index = ", buffer.maxVolumeIndex
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """

        request_type = 5
        speaker_descriptor_index = 1
        buffer = desc.CURRENT_COEFFICIENT_INFO()
        status = self.target.SWM_Network_SpeakerInfo(slave_index,
            request_type,
            speaker_descriptor_index,
            read_from_network,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def netstat(self, reset):
        """
        Retrieves current system transmit quality network statistics

        | Arguments:
        |  reset -- clears accumulated statistics (0 = no action, 1 = clears data)
        |
        | Returns:
        |  status -- system status code
        |  buffer -- system network quality struct defined by typedef NETWORK_TX_STATISTICS
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  buffer = desc.NETWORK_TX_STATISTICS()
        |  (status, buffer) = Tx.netstat(0)
        |  print "Status = ", status, "CRC Errors = ", buffer.CRCErrors
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x02
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Master Information Query command
        """

        type = 7
        buffer = desc.NETWORK_TX_STATISTICS()
        status = self.target.SWM_Master_GetMasterDescriptorInfo(type, reset, ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def get_volume_trim(self, device_id):
        """
        Get log volume trim value from a speaker

        | Arguments
        |  device_id
        |
        | Returns
        |  status -- system status code
        |  volume_trim -- amount of volume trim applied
        |
        | Example
        |  from pysummit.device import TxAPI
        |  Tx = TxAPI()
        |  (status, vol_trim) = Tx.get_volume_trim(0)
        |  print status, vol_trim
        |
        | Opcodes:
        |  Summit SWM908 API Specification, Network Messages
        """
        assert isinstance(device_id, int)
        volume_trim  = ctypes.c_int16(0)
        status = self.target.SWM_Network_LogVolumeTrimGet(
            device_id,
            ctypes.byref(volume_trim))
        return (status, volume_trim.value)

    @trace
    @retry_datalog
    def set_volume_trim(self, device_id, volume_trim):
        """
        Request master to send a command to apply a new log volume trim to a speaker

        | Arguments
        |  device_id
        |  volume_trim -- amount of volume trim to apply to device_id
        |
        | Returns
        |  status -- system status code
        |
        | Example
        |  from pysummit.device import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.set_volume_trim(0, -6)
        |  print status
        |
        | Opcodes:
        |  Summit SWM908 API Specification, Network Messages
        """
        assert isinstance(device_id, int)
        assert isinstance(volume_trim, int)
        status = self.target.SWM_Network_LogVolumeTrimSet(device_id, volume_trim)
        return (status, None)

#==============================================================================
# Remote I2C Methods
#==============================================================================

    @trace
#    @datalog
    @retry_datalog
    def remote_i2c_xfer(self, slave_id, dev_addr, reg_addr, bytes, data, reg_addr_bytes=2, clock_rate=400000):
        """
        Sends I2C command to specified slave

        | Arguments:
        |  slave_id -- specifies speaker to read (0 to 10)
        |  dev_addr -- I2C device address
        |  reg_addr -- I2C register address
        |  bytes    -- number of bytes to transfer
        |  data     -- data to transfer
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  data = ['0x05' '0xFE']
        |  (status, value) = Tx.remote_i2c_xfer(0, 0x03, 0x0, 2, data)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x11
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Remote I2C Transfer Command command
        """

        assert isinstance(slave_id, int)
        assert isinstance(dev_addr, int)
        assert isinstance(reg_addr, int)
        assert isinstance(bytes, int)
        assert isinstance(clock_rate, int)
        c_ubyte_array = (ctypes.c_ubyte * bytes)
        c_buffer = c_ubyte_array()
        if (0 == (1 & dev_addr)):  # Write
            for i in range(bytes):
                c_buffer[i] = data[i]
        status = self.target.SWM_Network_I2CXfer_With_Options(slave_id,
                                                   dev_addr,
                                                   reg_addr,
                                                   bytes,
                                                   ctypes.byref(c_buffer),
                                                   reg_addr_bytes,
                                                   clock_rate)
        return (status, None)


    @trace
#    @datalog
    @retry_datalog
    def remote_i2c_read_buf(self, slave_id):
        """
        Reads I2C data from speaker if available (as indicated by previous I2C Transfer Command)

        | Arguments:
        |  slave_id  -- specifies speaker to read (0 to 10)
        |  num_bytes -- number of bytes to read (0 to 80)
        |
        | Returns:
        |  status -- system status code
        |  value  -- [bytes_read, data read]
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.remote_i2c_read_buf(0, 8)
        |  print "Status = ", status,  value
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x13
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Remote I2C Transfer Read Data Buffer command
        """
        assert isinstance(slave_id, int)
        MAX_I2C_BUFFER_SIZE = 80
        c_bytes_read = ctypes.c_uint()
        c_ubyte_array = (ctypes.c_ubyte * MAX_I2C_BUFFER_SIZE)
        c_buffer = c_ubyte_array()
        status = self.target.SWM_Network_I2CReadBuffer(int(slave_id),
                                                         ctypes.byref(c_bytes_read),
                                                         ctypes.byref(c_buffer))
        buf = list()
        for i in range(c_bytes_read.value):
            buf.append(c_buffer[i])
        return (status, [c_bytes_read.value, buf])


    @trace
    @retry_datalog
    def remote_i2c_status(self, slave_id):
        """
        Querys specified speaker for status of previous I2C Transfer Command

        | Arguments:
        |  slave_index -- specifies speaker to query (0 to 10)
        |
        | Returns:
        |  status -- system status code
        |  value  -- I2C Status
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.remote_i2c_status(0)
        |  print "Status = ", status, "I2C status = ", value
        |
        | Opcodes:
        |  Main: 0x20, Secondary: 0x12
        |
        | See also:
        |  Summit SWM908 API Specification, Network Messages, Remote I2C Transfer Status Query command
        """

        c_xfer_status = ctypes.c_ubyte(0)
        status = self.target.SWM_Network_I2CStatus(int(slave_id), ctypes.byref(c_xfer_status))
        return (status, c_xfer_status.value)

#==============================================================================

    @trace
    @retry_datalog
    def save_configuration(self, type=0):
        """
        Saves system configuration data of master and slaves in their flash memory

        | Arguments:
        |  type -- configuration data (0 = save current configuration, 1 = restore factory default)
        |
        | Returns:
        |  status -- system status code
        |  speaker_map  -- list containing all speaker locations
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.save_configuration(0)
        |  print status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x0A
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Save System Configuration command
        """

        status = self.target.SWM_Master_SaveConfiguration(type)
        return (status, None)


#    def transmit_packets(self, num_packets):
#        status = self.target.DiagDriverTx(num_packets)
#        return (status, None)

#==============================================================================
# Zone Methods
#==============================================================================

    @trace
    @retry_datalog
    def get_speaker_zone(self):
        """
        Returns the current speaker zone.

        | Arguments: none
        |
        | Returns
        |  status -- system status code
        |  value  -- current zone
        |
        | Example:
        |
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.get_speaker_zone()
        |  print status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x11
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Zone Commands
        """

        zone = ctypes.c_ubyte(0)

        status = self.target.SWM_Master_GetSpeakerZone(ctypes.byref(zone))
        if(status == 0x01):
            self['zone'] = zone.value

        return (status, zone.value)

    @trace
    @retry_datalog
    def set_speaker_zone(self, zone):
        """
        Sets the current speaker zone.

        | Arguments:
        |  zone -- the zone number
        |
        | Returns
        |  status -- system status code
        |  value  -- none
        |
        | Example:
        |
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.set_speaker_zone(3)
        |  print status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x11
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Zone Commands
        """

        assert isinstance(zone, int)

        status = self.target.SWM_Master_SetSpeakerZone(zone)
        if(status == 0x01):
            self['zone'] = zone
        return (status, None)

    @trace
    @retry_datalog
    def move_speaker_zone(self, slave_id, zone):
        """
        Move an RX device to a different speaker zone.

        | Arguments:
        |  slave_id -- slave index
        |  zone     -- the zone to which the device will be moved
        |
        | Returns
        |  status -- system status code
        |  value  -- none
        |
        | Example:
        |
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.move_speaker_zone(3)
        |  print status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x11
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Zone Commands
        """
        assert isinstance(slave_id, int)
        assert isinstance(zone, int)

        status = self.target.SWM_Network_MoveSpeakerZone(slave_id, zone)

        return (status, None)

    @trace
    @retry_datalog
    def set_ir_filter(self, address):
        """
        Set software IR filter device address

        | Arguments:
        |  address -- manufacturing device address
        |
        | Returns
        |  status -- system status code
        |  value  -- none
        |
        | Example:
        |
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.set_ir_filter(0xFFEE)
        |  print status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x16
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Zone Commands
        """
        assert isinstance(address, int)

        status = self.target.SWM_Master_SetSoftIRFilter(address)
        return (status, None)

    @trace
    @retry_datalog
    def set_rx_control(self, enable):
        """
        Enable/disable @RX control

        | Arguments
        |  enable -- 0=disable, 1=enable
        |
        | Returns
        |  status -- system status code
        |  value -- none
        |
        | Example
        |
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.set_rx_control(1)
        |  print status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x17
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Zone Commands
        """
        assert isinstance(enable, int)
        status = self.target.SWM_Master_SetRxControl(enable)

        return (status, None)

    @trace
    @retry_datalog
    def set_max_zone(self, zone):
        """
        Set max zone accepted by @RX

        | Arguments
        |  value -- 0-7 indicating max zone supported by application
        |
        | Returns
        |  status -- system status code
        |  value -- none
        |
        | Example
        |
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.set_max_zone(1)
        |  print status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x17
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Zone Commands
        """
        assert isinstance(zone, int)
        status = self.target.SWM_Master_SetMaxZone(zone)

        return (status, None)

#==============================================================================
# LED Methods
#==============================================================================
    @trace
    @retry_datalog
    def set_led_disable(self, disable):
        """
        Disable/enable lighting of the Isoch and Heartbeat LEDs.

        | Arguments
        |  disable -- 1=disable(turns LEDs off), 0=enable
        |
        | Returns
        |  status -- system status code
        |  value -- none
        |
        | Example
        |
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.set_led_disable(1)
        |  print status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x19
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, LED Commands
        """
        assert isinstance(disable, int)
        status = self.target.SWM_Master_SetLedDisable(disable)

        return (status, None)

    @trace
    @datalog
    def get_led_disable(self):
        """
        Returns the state of the LED Disable flag

        | Arguments: none
        |
        | Returns:
        |  status  -- system status code
        |  disable -- state of the LED disable flag:
        |             1=disable(turns LEDs off), 0=enable
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, disable) = Tx.get_led_disable()
        |  print "Status = ", status, "LED Disable= ", disable
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x19
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, LED Commands
        """

        disable = ctypes.c_ubyte()
        status = self.target.SWM_Master_GetLedDisable(ctypes.byref(disable))
        return (status, disable.value)

#==============================================================================
# Autostart Methods
#==============================================================================
    @trace
    @retry_datalog
    def autostart(self, enable):
        """
        Enable/disable autostart on subsequent reboots.

        | Arguments
        |  enable -- 0=disable, 1=enable
        |
        | Returns
        |  status -- system status code
        |  value -- none
        |
        | Example
        |
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.autostart(1)
        |  print status
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x13
        |
        | See also:
        |  Summit SWM908 API Specification, Master Messages, Zone Commands
        """
        assert isinstance(enable, int)
        status = self.target.SWM_Master_SetAutoStart(enable)

        return (status, None)

#==============================================================================
# Multi-Master Methods
#==============================================================================
    @trace
    @retry_datalog
    def add_master_mac(self, device_index, mac):
        """
        Add an alternate TX device MAC to an RX device.

        | Arguments
        |  device_index
        |  mac -- list of bytes representing a MAC address [XX,XX,XX,XX,XX,XX]
        |
        | Returns
        |  status -- system status code
        |
        | Example
        |  from pysummit.device import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.add_master_mac(0, [02,EA,00,00,00,01])
        |  print status
        |
        | Opcodes:
        |  Summit SWM908 API Specification, Network Messages
        """
        assert isinstance(device_index, int)
        assert isinstance(mac, list)
        buffer = (ctypes.c_ubyte * len(mac))(*mac)
        status = self.target.SWM_Network_AddMasterMac(device_index, buffer)
        return (status, None)

    @trace
    @retry_datalog
    def remove_master_mac(self, slave_id, mac):
        """
        Remove a TX device MAC address from an RX device

        | Arguments
        |  device_index
        |  mac -- list of bytes representing a MAC address [XX,XX,XX,XX,XX,XX]
        |
        | Returns
        |  status -- system status code
        |
        | Example
        |  from pysummit.device import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.remove_master_mac(0, [02,EA,00,00,00,01])
        |  print status
        |
        | Opcodes:
        |  Summit SWM908 API Specification, Network Messages
        """
        assert isinstance(slave_id, int)
        assert isinstance(mac, list)
        buffer = (ctypes.c_ubyte * len(mac))(*mac)
        status = self.target.SWM_Network_RemoveMasterMac(slave_id, buffer)
        return (status, None)

    @trace
    @retry_datalog
    def get_master_macs(self, slave_id):
        """
        List all TX device MACs known by the RX device.

        | Arguments
        |  device_index
        |
        | Returns
        |  status -- system status code
        |  macs -- a list of MAC addresses
        |
        | Example
        |  from pysummit.device import TxAPI
        |  Tx = TxAPI()
        |  (status, macs) = Tx.get_master_macs(0)
        |  print status
        |
        | Opcodes:
        |  Summit SWM908 API Specification, Network Messages
        """
        assert isinstance(slave_id, int)
        buffer = (desc.MAC_ADDRESS * 4)()
        number_macs   = ctypes.c_ubyte()
        status = self.target.SWM_Network_GetMasterMac(
            slave_id,
            ctypes.byref(buffer),
            ctypes.byref(number_macs))
        return (status, map(str,buffer[:number_macs.value]))

    @trace
    @retry_datalog
    def assign_master_mac(self, slave_id, master_number):
        """
        Assign a new TX device MAC for the RX device to respond to.

        | Arguments
        |  device_index
        |  master_number -- index number of available TX device macs
        |
        | Returns
        |  status -- system status code
        |
        | Example
        |  from pysummit.device import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.assign_master_mac(0, "02:EA:00:00:00:01")
        |  print status
        |
        | Opcodes:
        |  Summit SWM908 API Specification, Network Messages
        """
        assert isinstance(slave_id, int)
        assert isinstance(master_number, int)
        status = self.target.SWM_Network_AssignMasterMac(slave_id, master_number)
        return (status, None)


    @trace
    @retry_datalog
    def set_block_events_enable(self, enable):
        """
        Ask master to enable blocking of certain key slave events.

        | Arguments
        |  enable -- 0 for disable, else enable
        |
        | Returns
        |  status -- system status code
        |
        | Example
        |  from pysummit.device import TxAPI
        |  Tx = TxAPI
        |  status = Tx.set_block_events_enable(1)
        |  print status
        """
        assert isinstance(enable, int)
        status = self.target.SWM_Master_KeySpeakerEventsBlockEnable(enable)
        return (status, None)


    @trace
    @retry_datalog
    def get_block_events_enable(self):
        """
        Ask master to return the events blocking enable value

        | Arguments
        |  enable -- 0 for disable, else enable
        |
        | Returns
        |  status -- system status code
        |
        | Example
        |  from pysummit.device import TxAPI
        |  Tx = TxAPI
        |  status = Tx.set_block_events_enable(1)
        |  print status
        """
        enable = ctypes.c_ubyte()
        status = self.target.SWM_Master_KeySpeakerEventsBlockEnableGet(ctypes.byref(enable))
        return (status, enable.value)

#==============================================================================
# TxAPI Convenience Methods
#==============================================================================
    @trace
#    @retry_datalog
    def push_map_profile(self, profile, count=1, preload=True):
        """
        Loads speaker mapping based on configuration info defined in test profile file

        | Arguments:
        |  profile -- test profile loaded via testprofile.TestProfile()
        |  count   -- number of slaves to push (1 pushes them one at a time)
        |  preload -- not used
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  import pysummit.testprofile
        |
        |  Tx = TxAPI()
        |  profile = testprofile.TestProfile()
        |  profile.readfp(open('/home/username/mysys.cfg'))
        |  (status, null) = Tx.push_map_profile(profile, 8)
        |  print status
        """
        assert (count < 33)  # Max number of speakers is 32 for now
        slave_index = 0x00
        status = 0x02  # this should get overwritten (INVALID_CMD)
        add_log = signal('datalog_add')
        tp = profile

        if(tp):
            (status, slave_count) = self.slave_count()
            if(status != 0x01):
                self.decode_error_status(status, cmd='get_slave_count')

            (i2s_map, speaker_map) = self.make_speaker_map(slave_count, tp)
            if(slave_count != len(speaker_map)):
                logging.error("The number of discovered RX devices does not match the number defined in the test profile. They must be equal.")
                return

            (status, null) = self.set_i2s_input_map(i2s_map)
            if(status != 0x01):
                self.decode_error_status(status, cmd='set_i2s_input_map')

            sys.stdout.write('\n')
            sys.stdout.write('Pushing')
            if (count == 1):  # Push one speaker map at a time (pre-194.2)
                for slave_index in range(slave_count):
                    sys.stdout.write('.')
                    sys.stdout.flush()
                    (status, null) = self.push_map(slave_index, speaker_map[slave_index], count)
                    if (slave_index == slave_count-1): # last spkr should get 0x01 status
                        exp = 0x01
                    else:
                        exp = 0x9B
                    add_log.send(self['mac'], name='push_map', exp="0x%.2X"%exp, act="0x%.2X"%status)
                    if (status != 0x01 and status != 0x9B):
                        logging.error(self.decode_error_status(status, cmd='push_map(%d, )' % 0))
            else:
                map_info_arr = desc.SPEAKER_MAP_INFO * slave_count
                map_info = map_info_arr()
                for slave_index in range(slave_count):
                    map_info[slave_index] = speaker_map[slave_index]

                (status, null) = self.push_map(0, map_info[0], len(map_info))
                add_log.send(self['mac'], name='push_multi_map', exp="0x%.2X"%0x01, act="0x%.2X"%status)
                if (status != 0x01):
                    logging.error(self.decode_error_status(status, cmd='push_multi_map(%d, )' % 0))
            sys.stdout.write('\n')
            sys.stdout.flush()
        return (status, speaker_map)

    @trace
    def make_speaker_map(self, slave_count, test_profile):
        """
        Generate I2S and speaker map structures and return them

        | Arguments:
        |  slave_count  -- number of speakers to include in map
        |  test_profile -- configuration file specifiying test setup
        |
        | Returns:
        |  i2s_map_inst -- I2S map
        |  speaker_map  -- speaker map
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  tp = Tx.load_profile('/home/username/mysys.cfg')
        |  (i2s_map, speaker_map) = Tx.make_speaker_map(2, tp)
        |  print i2s_map
        |
        | Opcodes:
        |  Main: 0x10, Secondary: 0x0A
        |
        | See also:
        |  set_i2s_input_map(), push_map()
        """

        speaker_map = []
        slave_macs = []

        I2S_MAP = desc.SPEAKER_TYPE_TO_I2S_MAP * 11
        i2s_map_inst = I2S_MAP()
        slot_map = {
            1: (0,0),  # slot 1
            2: (0,1),  # slot 2
            3: (1,0),  # slot 3
            4: (1,1),  # slot 4
            5: (2,0),  # slot 5
            6: (2,1),  # slot 6
            7: (3,0),  # slot 7
            8: (3,1)   # slot 8
        }

        # If section [I2S] exists in the test_profile use it to make an
        # explicit I2S input map
        types_to_slots = {}
        map_index = 0
        if(test_profile.has_section('I2S')):
            self.logger.warning("Generating I2S input map from [I2S] section of test profile.")
            self.logger.warning("Explicit slot assignments will be ignored.")
            for slot,types in test_profile.items('I2S'):
                slot = int(slot,0)
                for stype in types.split():
                    stype = int(stype,0)
                    types_to_slots[stype] = slot
                    if(map_index > 11):
                        self.logger.error("Too many entries in the [I2S] section of the test profile. Max is 11")
                        return (i2s_map_inst, speaker_map)
                    i2s_map_inst[map_index].codecI2SChannel = slot_map[slot][0]
                    i2s_map_inst[map_index].codecChannel    = slot_map[slot][1]
                    i2s_map_inst[map_index].speakerType     = stype
                    map_index += 1

        # Gather discovered MACs
        for slave_index in range(slave_count):
            (status, smd) = self.get_speaker_module_descriptor(slave_index)
            if(status != 0x01):
                self.decode_error_status(status, cmd='get_speaker_module_descriptor(%s)' % slave_index)
                return (i2s_map_inst, speaker_map)
            mac = ":".join(["%.2X" % i for i in smd.macAddress])
            slave_macs.append(mac)

        print ""
        print("{:20}   {:<5}   {:<6} {:<6}   {:<6}   {:4}".format(
            "Device",
            "Slot",
            "X",
            "Y",
            "Vector",
            "Type",
        ))
        term_columns, sizey = terminalsize.get_terminal_size()
        print("-"*term_columns)

        map_index = 0
        old_macs = []
        for mac in slave_macs:
            section = "RX %s" % mac
            if(test_profile.has_section(section)):
                if (mac in old_macs):
                    if (test_profile.has_option(section, 'xy2')):
                        xy = test_profile.get(section, 'xy2')
                    if (test_profile.has_option(section, 'speaker_type2')):
                        speaker_type = test_profile.get(section, 'speaker_type2')
                else:
                    xy = test_profile.get(section, 'xy')
                    speaker_type = test_profile.get(section, 'speaker_type')

                (x,y) = xy.split(',')
                x = int(x)
                y = int(y)
                vector_distance = int(math.sqrt(x**2+y**2))
                speaker_type = int(speaker_type, 0)

                # Make I2S map based on speaker type and slot if the [I2S]
                # section does not exist in the test profile
                if(not test_profile.has_section('I2S')):
                    if (mac in old_macs):
                        if(test_profile.has_option(section, 'slot2')):
                            slot = int(test_profile.get(section, 'slot2'),0)
                            i2s_map_inst[map_index].codecI2SChannel = slot_map[slot][0]
                            i2s_map_inst[map_index].codecChannel    = slot_map[slot][1]
                            i2s_map_inst[map_index].speakerType     = speaker_type
                    elif(test_profile.has_option(section, 'slot')):
                        slot = int(test_profile.get(section, "slot"),0)
                        i2s_map_inst[map_index].codecI2SChannel = slot_map[slot][0]
                        i2s_map_inst[map_index].codecChannel    = slot_map[slot][1]
                        i2s_map_inst[map_index].speakerType     = speaker_type
                    else:
                        slot = types_to_slots.get(speaker_type, 'none')
                    map_index += 1
                else:
                    slot = types_to_slots.get(speaker_type, 'none')

                print("{:20}   {:<5}   {:<+6} {:<+6}   {:<6}   {:#04X} ({:})".format(
                    section,
                    slot,
                    x,
                    y,
                    vector_distance,
                    speaker_type,
                    dec.speaker_types.get(speaker_type, "INVALID")
                ))

                speaker = desc.SPEAKER_MAP_INFO()
                speaker.speakerX = x
                speaker.speakerY = y
                speaker.speakerVectorDistance = vector_distance
                speaker.speakerType = speaker_type

                speaker_map.append(speaker)

                if (mac not in old_macs):
                    old_macs.append(mac)
        return (i2s_map_inst, speaker_map)

    @trace
    @retry_datalog
    def disco(self, beacon_time=4500, radio_channel=99, restore=True):
        """
        Combined beacon discover/restore command for network startup

        | Arguments:
        |  time    -- beacon transmit time as milliseconds (default = 4.5s)
        |  channel -- radio channel (default = 99)
        |  restore -- (0 to do full discovery, 1 to restore stored configuration)
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, value) = Tx.disco()
        |  print status
        |
        | See also:
        |  beacon(), discovery(), restore()
        """

        self.logger.info("beacon(%d,%d)" % (radio_channel, beacon_time))
        (b_status, null) = self.beacon(beacon_time, radio_channel)
        if(restore):
            self.logger.info("restore()")
            (d_status, null) = self.restore()
        else:
            self.logger.info("discover(1)")
            (d_status, null) = self.discover(1)

        return (d_status, None)

    @increase_timeout(10)
    def invoke_radio_cal_state(self, state, measurement=None):
        """Calibrate an Olympus based module.

        | Arguments:
        |  state -- enumerated value from RADIO_CAL_SM_STATE to be invoked
        |  measurement -- prior state's RF power measurement
        |
        | Returns:
        |  status  -- radio cal status code
        |  cal_sm_state -- enumerated state value from RADIO_CAL_STATUS

        """
        cal_sm_state = ctypes.c_ubyte(state)

        if(measurement == None):
            radio_cal_status = self.target.OlyInvokeRadioCalState(ctypes.byref(cal_sm_state), None)
        else:
            cal_meas = ctypes.c_double(measurement)
            radio_cal_status = self.target.OlyInvokeRadioCalState(ctypes.byref(cal_sm_state), ctypes.byref(cal_meas))
        return (radio_cal_status, cal_sm_state.value)


class RxAPI(API):
    """
    PySummit system functions specific to control of Slave devices

    """

    def __init__(self, coms=None, name="Slave"):
        # Setup function pointers
        lib_filename = resource_filename(__name__,"SWMRXAPI.so")
#        lib_filename = resource_filename(Requirement.parse("pysummit"),"SWMRXAPI.so")
        super(RxAPI, self).__init__(ctypes.CDLL(lib_filename), name)
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.getLogger().level)
        self.__devs = []
        self.__com_index = -1
        self.open_func = self.ACCESS_FUNC(self._py_open_func)
        self.close_func = self.ACCESS_FUNC(self._py_close_func)
        self.wr_func = self.IO_FUNC(self._py_wr_func)
        self.rd_func = self.IO_FUNC(self._py_rd_func)
        self.open()
        if(coms):
            self.set_coms(coms)

        # Create a combined dictionary with both Summit status and custom I/O status
        self.status_codes = {}
        self.status_codes.update(dec.system_status_rx)
        self.status_codes.update(dec.serial_status)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close_coms()

    def __getitem__(self, index):
        if(type(index) == type(1)):
            if(index < len(self)):
                self.__com_index = index
                return self
            else:
                raise IndexError
        elif(re.match('..:..:..:..:..:..', index)):
            self.__com_index = self.index(index)
            return self
        else:
            if(self.__com_index < len(self)):
                return self.__devs[self.__com_index][index]
            else:
                raise IndexError

    def __setitem__(self, index, value):
        self.__devs[self.__com_index][index] = value

    def __iter__(self):
        self.__com_index = -1
        return self

    def next(self):
        """
        Returns next instance of slave device
        """

        self.__com_index += 1
        if(self.__com_index >= len(self.__devs)):
            self.__com_index = -1
            raise StopIteration
        return self

#    def __getitem__(self, index):
#        self._set_port(index)
#        return self

    def __len__(self):
        return len(self.__devs)

    def __contains__(self, item):
        """Membership testing via MACs"""
        for dev in self:
            if(item == self['mac']):
                return True
        return False

    def index(self, mac):
        """
        Returns the com_index given the MAC

        | Arguments:
        |  mac -- MAC address specifiying device
        |
        | Returns:
        |  index -- device index associated with specified MAC
        |
        | Example:
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  print RX.index("02:EA:4C:00:00:13")
        |
        | See also:
        |  get_our_mac(), id()
        """

        for dev in self:
            if(mac == self['mac']):
                return self.__com_index

#    def _set_port(self, index):
#        if(index > len(self.__devs)-1):
#            raise IndexError("Device index is out of range")
#        else:
#            self.com_index = index

    def _prune_devs(self):
        """
        Checks status of all connected devices, removes those that fail to respond
        """
        new_devs = []
        dev_count = 0
        print "Checking serial ports for Summit RX devices..."
        for dev in self:
            if not dev['com'].connect():
                continue
            # Do a quick check for OUR_MAC0. Try to not flood the connected
            # device with data, it may not be a Summit device.
            prev_retry_count = self.get_retries()
            self.set_retries(1)
            (rd_status, our_mac0) = dev.rd(0x403024)
            self.set_retries(prev_retry_count)

            if((rd_status == 0x01) & (our_mac0 == 0xEA02)):
                (smd_status, smd) = dev.get_speaker_module_descriptor()
                (sd_status, sd) = dev.get_speaker_descriptor()
                logging.debug("smd_status: %d" % smd_status)
                logging.debug("sd_status: %d" % sd_status)
                logging.debug("smd.hardwareType: %d" % smd.hardwareType)
                if((smd_status == 0x01) and (sd_status == 0x01)):
                    major = smd.firmwareVersion >> 5   # (Upper 11-bits)
                    minor = smd.firmwareVersion & 0x1f # (Lower 5-bits)
                    dev['fw_major'] = major
                    dev['fw_minor'] = minor
                    dev['fw_version'] = "%d.%d" % (major, minor)
                    dev['mac'] = ":".join(["%.2X" % i for i in smd.macAddress])
                    dev['index'] = dev_count
                    dev['speaker_type'] = sd.staticSpeakerType
                    dev['type'] = 'slave'
                    dev['module_id'] = smd.moduleID
                    new_devs.append(self.__devs[self.__com_index])
                    dev_count += 1
                    self.start_logging()
                    print "[%s] %s" % (colored('*', 'green'), dev['port'])
                else:
                    print "[ ] %s" % (dev['port'])
                    dev['com'].write('\n\n')
                    self.close()
            else:
                logging.debug("Removing com %s" % dev['port'])
                print "[ ] %s" % dev['port']
                self.close()

        return list(new_devs)

    @trace
    def get_timeout(self):
        """
        Returns current command timeout value

        | Arguments: none
        |
        | Returns:
        |  timeout -- command timeout value
        |
        | Example:
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  value = Rx.get_timeout()
        |  print value
        """

#        return self.__devs[self.__com_index]['com'].target.timeout
        return self['com'].target.timeout

    @trace
    def set_timeout(self, timeout):
        """
        Sets command timeout value

        | Arguments:
        |  timeout -- command timeout value in seconds
        |
        | Returns: none
        |
        | Example:
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  Rx.set_timeout(3)
        |
        """

#        self.__devs[self.__com_index]['com'].target.timeout = timeout
        self['com'].target.timeout = timeout

    @trace
    def close_coms(self):
        for dev in self:
            logging.debug("Closing current com ports")

            dev['com'].close()

    @trace
    def set_coms(self, coms, prune_devs=True, logging_enable=False):
        """
        Closes open com ports and initializes ports in coms list

        | Arguments:
        |  coms           -- list of com ports
        |  prune_devs     -- remove non responsive devices (0 = no action, 1 = prune)
        |  logging_enable -- enable file logging of serial output
        |
        | Returns: none
        |
        | Example:
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  Rx.set_coms(coms)
        """

        for dev in self:
            logging.debug("Closing current com ports")
            dev['com'].close()
        self.__devs = []
        for com in coms:
            self.__devs.append(
                {   'index': 0,
                    'com': com,
                    'port': com.target.port,
                    'fw_major': "0.0",
                    'fw_minor': "0.0",
                    'fw_version': "0.0",
                    'mac': None,
                    'xy': (0,0),
                    'speaker_type': 0x00,
                    'logging': logging_enable,
                }
            )
        if(prune_devs):
            self.__devs = self._prune_devs()

    @trace
    def get_port(self):
        """
        Returns the name of the current serial port

        | Arguments: none
        |
        | Returns:
        |  port -- current serial port
        |
        | Example:
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  port = Rx.get_port()
        |  print "Port = ", port
        """

        return self['port']

    def start_logging(self):
        if(self['logging']):
            self['com'].start_logging(self['mac'])
        return

    def stop_logging(self):
        self['com'].stop_logging()
        return

    @trace
    def id(self):
        """
        Returns the ID of the currently selected RX device

        | Arguments: none
        |
        | Returns:
        |  id -- index of the currently selected device
        |
        | Example:
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  value = Rx.id()
        |  print value
        """
#        return self.com_index+1 # +1 because the master is always device 0
#        return self.__com_index
        return self['index']

    @trace
    def open(self):
        """
        Opens Raspberry Pi communication with slave

        | Arguments: none
        |
        | Returns: None
        |
        | Example:
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  Rx.open()
        """

        super(RxAPI, self).open(self.wr_func, self.rd_func, self.open_func, self.close_func)

    @trace
    def close(self):
        """
        Closes Raspberry Pi communication with slave

        | Arguments: none
        |
        | Returns: None
        |
        | Example:
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  Rx.close()
        """

#        self.coms[self.com_index].close()
        self['com'].close()

    @increase_timeout(3)
    def reboot(self):
        """
        Reboots slave via Apollo asic registers

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  (status, None) = Rx.scanning()
        |  print "Status = " status
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Register Access command
        |  Olympus Register Specification Document
        """
        status1 = self.target.SWM_Diag_SetRegister(0x400040, 0x8000) # pio reset
        status2 = self.target.SWM_Diag_SetRegister(0x400040, 0x0000) # pio clear
        status3 = self.target.SWM_Diag_SetRegister(0x400064, 0x0100) # nios reset
        status = (status1 == 1) and (status2 == 1) and (status3 == 0xE2)
        return (status, None)

#==============================================================================
# Callback functions
#==============================================================================
    def _py_wr_func(self, mes):
        status = 0x0
        self['com'].lock_port()
        try:
            if(self['com'].isOpen()):
                message = mes[0].to_pkt()
                bytes_written = self['com'].write(message)
                if(len(message) != bytes_written):
                    status = 0xE1
                else:
                    status = 0
        except:
            raise
        finally:
            self['com'].unlock_port()

        return status


    def _py_rd_func(self, mes):
        """Serial read method that searches for correct Summit protocol 1 byte
        at a time.

        """
        self['com'].lock_port()
        self['com'].target.flushInput()
        message = mes[0].to_pkt()

        bytes_written = self['com'].write(message)
        if(len(message) != bytes_written):
            self['com'].unlock_port()
            return 0xE1
        else:
            status = 0

        byte_count = 0
        while(True): # Read until exception or return
            byte = self['com'].read(1)  # Tries to read until timeout
            if(len(byte) != 1):
                return 0xE1

            if(byte_count == 0):
                if(ord(byte) == 0x01):
                    message = byte
                    byte_count += 1
            elif(byte_count > 0):
                if((ord(byte) == 0x01) & (ord(message[0]) == 0x01)):
                    message += byte
                    byte_count += 1
                    message += self['com'].read(7)
                    if(len(message) != 9):
                        return 0xE2
                    else:
                        data_len = ord(message[7]) + (ord(message[8])<<8)
                        message += self['com'].read(data_len)

                        try:
                            mes[0].from_pkt(message)
                        except TargetPacketError as info:
                            self['com'].unlock_port()
                            return 0xE4
                        except:
                            raise
                        if(len(message) != (data_len+9)):
                            self['com'].unlock_port()
                            return 0xE3 # READ_PAYLOAD_ERROR

                        self['com'].unlock_port()
                        return status

    def _py_open_func(self):
        return 0

    def _py_close_func(self):
        return 0

    def _py_reset_func(self):
        return 0

#==============================================================================
# API Methods
#==============================================================================
    @trace
    @retry_datalog
    def get_speaker_operating_state(self):
        """
        Retrieves speaker's current operational data state

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  buffer -- slave operating state struct defined by typedef SPEAKER_OPERATING_STATE
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.SPEAKER_OPERATING_STATE()
        |  (status, buffer) = Rx.get_speaker_operating_state()
        |  print "Status = ", status, "Speaker mode = ", buffer.speakerMode
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        request_type = 0
        speaker_descriptor_index = 0
        buffer = desc.SPEAKER_OPERATING_STATE()
        status = self.target.SWM_Diag_SpeakerInfo(request_type,
            speaker_descriptor_index,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def set_speaker_operating_state(self, buffer):
        """
        Sets speaker's current operational data state

        | Arguments:
        |  buffer -- slave operating state struct defined by typedef SPEAKER_OPERATING_STATE
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.SPEAKER_OPERATING_STATE() ## load the fields...
        |  (status) = Rx.set_speaker_operating_state(0, buffer)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        assert isinstance(buffer, desc.SPEAKER_OPERATING_STATE)
        assert ctypes.sizeof(buffer) == ctypes.sizeof(desc.SPEAKER_OPERATING_STATE)
        request_type = 0
        speaker_descriptor_index = 0
        status = self.target.SWM_Diag_SetSpeakerInfo(request_type,
            speaker_descriptor_index,
            ctypes.byref(buffer),
            ctypes.sizeof(buffer))
        return (status, None)

    @trace
    @retry_datalog
    def get_speaker_module_descriptor(self):
        """
        Retrieves speaker module descriptor data from slave's flash memory

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  buffer -- module descriptor struct defined by typedef MODULE_DESCRIPTOR
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.MODULE_DESCRIPTOR()
        |  (status, buffer) = Rx.get_speaker_module_descriptor()
        |  print "Status = ", status, "Vendor ID = ", buffer.vendorID
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        request_type = 1
        speaker_descriptor_index = 0
        buffer = desc.MODULE_DESCRIPTOR()
        status = self.target.SWM_Diag_SpeakerInfo(request_type,
            speaker_descriptor_index,
            ctypes.byref(buffer))
        return (status, buffer)


    @trace
    @retry_datalog
    def set_speaker_module_descriptor(self, buffer):
        """
        Set speaker module descriptor data to slave's flash memory

        | Arguments:
        |  buffer -- module descriptor struct defined by typedef MODULE_DESCRIPTOR
        |
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.MODULE_DESCRIPTOR()  ## update fields...
        |  (status, buffer) = Rx.set_speaker_module_descriptor(0, buffer)
        |  print "Status = ", status, "Vendor ID = ", buffer.vendorID
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        assert isinstance(buffer, desc.MODULE_DESCRIPTOR)
        assert ctypes.sizeof(buffer) == ctypes.sizeof(desc.MODULE_DESCRIPTOR)
        request_type = 1
        speaker_descriptor_index = 0
        status = self.target.SWM_Diag_SetSpeakerInfo(request_type,
            speaker_descriptor_index,
            ctypes.byref(buffer),
            ctypes.sizeof(buffer))
        return (status, None)

    @trace
    @retry_datalog
    def get_speaker_descriptor(self, speaker_descriptor_index=0):
        """
        Retrieves speakers descriptor data from slave's flash memory

        | Arguments: none
        |  speaker_descriptor_index -- each side of stereo amp (0 - 1)
        |
        | Returns:
        |  status -- system status code
        |  buffer -- amplifier descriptor struct defined by typedef SPEAKER_DESCRIPTOR
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.SPEAKER_DESCRIPTOR()
        |  (status, buffer) = Rx.get_speaker_descriptor()
        |  print "Status = ", status, "s/n = ", buffer.serialNumber
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        assert isinstance(speaker_descriptor_index, int)
        request_type = 2
        buffer = desc.SPEAKER_DESCRIPTOR()
        status = self.target.SWM_Diag_SpeakerInfo(request_type,
            speaker_descriptor_index,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def set_speaker_descriptor(self, speaker_descriptor_index, buffer):
        """
        Sets speakers descriptor data to slave's flash memory

        | Arguments: none
        |  speaker_descriptor_index -- each side of stereo amp (0 or 1)
        |  buffer -- speaker descriptor struct defined by typedef SPEAKER_DESCRIPTOR
        |
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.SPEAKER_DESCRIPTOR()  ## and set the fields
        |  (status) = Rx.set_speaker_descriptor(0, buffer)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        assert isinstance(speaker_descriptor_index, int)
        assert isinstance(buffer, desc.SPEAKER_DESCRIPTOR)
        assert ctypes.sizeof(buffer) == ctypes.sizeof(desc.SPEAKER_DESCRIPTOR)
        request_type = 2
        status = self.target.SWM_Diag_SetSpeakerInfo(request_type,
            speaker_descriptor_index,
            ctypes.byref(buffer),
            (ctypes.sizeof(buffer) - ctypes.sizeof(desc.AMPLIFIER_DESCRIPTOR)))
        return (status, None)

    @trace
    @retry_datalog
    def get_speaker_wisa_descriptor(self):
        """
        Retrieves speakers Wisa descriptor from slave's flash memory

        | Arguments: none
        |  buffer -- descriptor struct defined by typedef WISA_DESCRIPTOR
        |
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.WISA_DESCRIPTOR()
        |  (status, buffer) = Rx.get_speaker_wisa_descriptor()
        |  print "Status = ", status, "Version = ", buffer.wisaVersion
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        request_type = 3
        speaker_descriptor_index = 0
        buffer = desc.WISA_DESCRIPTOR()
        status = self.target.SWM_Diag_SpeakerInfo(request_type,
            speaker_descriptor_index,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def set_speaker_wisa_descriptor(self, buffer):
        """
        Set speakers Wisa descriptor to slave's flash memory

        | Arguments:
        |  buffer -- amplifier descriptor struct defined by typedef WISA_DESCRIPTOR
        |
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.WISA_DESCRIPTOR()
        |  (status) = Rx.set_speaker_wisa_descriptor(0, buffer)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        assert isinstance(buffer, desc.WISA_DESCRIPTOR)
        assert ctypes.sizeof(buffer) == ctypes.sizeof(desc.WISA_DESCRIPTOR)
        request_type = 3
        speaker_descriptor_index = 0
        status = self.target.SWM_Diag_SetSpeakerInfo(request_type,
            speaker_descriptor_index,
            ctypes.byref(buffer),
            ctypes.sizeof(buffer))
        return (status, None)

    @trace
    @retry_datalog
    def get_speaker_amplifier_descriptor(self, speaker_descriptor_index=0):
        """
        Retrieves amplifier descriptor data from slave's flash memory

        | Arguments: none
        |  speaker_descriptor_index -- each side of stereo amp (0 or 1)
        |
        | Returns:
        |  status -- system status code
        |  buffer -- amplifier descriptor struct defined by typedef AMPLIFIER_DESCRIPTOR
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.AMPLIFIER_DESCRIPTOR()
        |  (status, buffer) = Rx.get_speaker_amplifier_descriptor()
        |  print "Status = ", status, "Vendor ID = ", buffer.vendorID
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        assert isinstance(speaker_descriptor_index, int)
        request_type = 4
        buffer = desc.AMPLIFIER_DESCRIPTOR()
        status = self.target.SWM_Diag_SpeakerInfo(request_type,
            speaker_descriptor_index,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def set_speaker_amplifier_descriptor(self, speaker_descriptor_index, buffer):
        """
        Sets amplifier descriptor data to slave's flash memory

        | Arguments:
        |  speaker_descriptor_index -- each side of stereo amp (0 or 1)
        |  buffer -- amplifier descriptor struct defined by typedef AMPLIFIER_DESCRIPTOR
        |
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.AMPLIFIER_DESCRIPTOR()  # and set fields
        |  (status) = Rx.set_speaker_amplifier_descriptor(0, buffer)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        assert isinstance(speaker_descriptor_index, int)
        assert isinstance(buffer, desc.AMPLIFIER_DESCRIPTOR)
        request_type = 4
        status = self.target.SWM_Diag_SetSpeakerInfo(request_type,
            speaker_descriptor_index,
            ctypes.byref(buffer),
            (ctypes.sizeof(desc.AMPLIFIER_DESCRIPTOR) -
             ctypes.sizeof(desc.AMPLIFIER_CONFIGURATION)))
        return (status, None)

    @trace
    @retry_datalog
    def get_speaker_global_coefficient_info(self):
        """
        Retrieves the speaker's global coefficient data

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  buffer -- coefficient struct defined by typedef GLOBAL_COEFFICIENT_INFO
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.GLOBAL_COFFICIENT_INFO()
        |  (status, buffer) = Rx.get_speaker_global_coefficient_info()
        |  print "Status = ", status, "Min volume = ", buffer.speakerMinVolumeIndex
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        request_type = 5
        current_coefficient_data = 0  ## False; 0 = global_coefficient_data
        buffer = desc.GLOBAL_COEFFICIENT_INFO()
        status = self.target.SWM_Diag_SpeakerInfo(request_type,
            current_coefficient_data,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def set_speaker_global_coefficient_info(self, buffer):
        """
        Sets the speaker's global coefficient data

        | Arguments:
        |  buffer -- coefficient struct defined by typedef GLOBAL_COEFFICIENT_INFO
        |
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.GLOBAL_COFFICIENT_INFO()  ## and edit fields
        |  (status) = Rx.set_speaker_global_coefficient_info(buffer)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        assert isinstance(buffer, desc.GLOBAL_COEFFICIENT_INFO)
        assert ctypes.sizeof(buffer) == ctypes.sizeof(desc.GLOBAL_COEFFICIENT_INFO)
        request_type = 5
        current_coefficient_data = 0  ## False; 0 = global_coefficient_data
        status = self.target.SWM_Diag_SetSpeakerInfo(request_type,
            current_coefficient_data,
            ctypes.byref(buffer),
            ctypes.sizeof(buffer))
        return (status, None)

    @trace
    @retry_datalog
    def get_speaker_current_coefficient_info(self):
        """
        Retrieves the speaker's currently selected coefficient data

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  buffer -- coefficient struct defined by typedef CURRENT_COEFFICIENT_INFO
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.CURRENT_COFFICIENT_INFO()
        |  (status, buffer) = Rx.get_speaker_current_coefficient_info()
        |  print "Status = ", status, "Min volume = ", buffer.inVolumeIndex
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        request_type = 5
        current_coefficient_data = 1  ## True; 0 = global_coefficient_data
        buffer = desc.CURRENT_COEFFICIENT_INFO()
        status = self.target.SWM_Diag_SpeakerInfo(request_type,
            current_coefficient_data,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    @retry_datalog
    def set_speaker_current_coefficient_info(self, buffer):
        """
        Sets the speaker's currently selected coefficient data

        | Arguments:
        |  buffer -- coefficient struct defined by typedef CURRENT_COEFFICIENT_INFO
        |
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.CURRENT_COFFICIENT_INFO()  ## and edit fields
        |  (status) = Rx.set_speaker_current_coefficient_info(buffer)
        |  print "Status = ", status, "Min volume = ", buffer.inVolumeIndex
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        assert isinstance(buffer, desc.CURRENT_COEFFICIENT_INFO)
        assert ctypes.sizeof(buffer) == ctypes.sizeof(desc.CURRENT_COEFFICIENT_INFO)
        request_type = 5
        current_coefficient_data = 1  ## True; 0 = global_coefficient_data
        status = self.target.SWM_Diag_SetSpeakerInfo(request_type,
            current_coefficient_data,
            ctypes.byref(buffer),
            ctypes.sizeof(buffer))
        return (status, None)

    @trace
    @increase_timeout(5)
    @retry_datalog
    def save_speaker_mfg_data(self):
        """
        Saves MFG data into flash

        | Arguments: None
        |
        | Returns:
        |  status -- system status code
        |
        | Example:
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  (status) = Rx.save_speaker_mfg_data()
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x16
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status command
        """
        status = self.target.SWM_Diag_SaveMfgData()
        return (status, None)


    @trace
    @retry_datalog
    def get_fw_version(self):
        """
        Retrieves slave's firmware version from module descriptor in flash

        | Arguments: none
        |
        | Returns:
        |  status  -- system status code
        |  version -- firmware version
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  (status, version) = Rx.get_fw_version()
        |  print "Status = ", status, "f/w Version = ", version
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status Query command
        """

        request_type = 1
        speaker_descriptor_index = 0
        buffer = desc.MODULE_DESCRIPTOR()
        status = self.target.SWM_Diag_SpeakerInfo(request_type,
            speaker_descriptor_index,
            ctypes.byref(buffer))
        return (status, buffer.firmwareVersion)

    @trace
#    @retry_datalog
    def netstat(self, reset=0):
        """
        Retrieves current system receive quality network statistics

        | Arguments:
        |  reset -- clears accumulated statistics (0 = no action, 1 = clears data)
        |
        | Returns:
        |  status -- system status code
        |  buffer -- system network quality struct defined by typedef NETWORK_RX_STATISTICS
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  buffer = desc.NETWORK_RX_STATISTICS()
        |  (status, buffer) = Rx.netstat(0)
        |  print "Status = ", status, "CRC Errors = ", buffer.CRCErrors
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x0F
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Slave Status Query command
        """

        request_type = 7
        buffer = desc.NETWORK_RX_STATISTICS()
        status = self.target.SWM_Diag_SpeakerInfo(request_type,
            reset,
            ctypes.byref(buffer))
        return (status, buffer)

    @trace
    def erase_flash(self):
        """
        Erases slave's entire flash memory

        | Arguments: none
        |
        | Returns:
        |  status -- system status code
        |  value  -- None
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  (status, null) = Rx.erase_flash()
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x03
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Erase command
        """

        status = self.target.SWM_Diag_EraseFlash()
        return (status, None)

    @trace
    @retry_datalog
    def get_coefficient_data(self):
        """
        Retreives coefficient data from slave's flash memory

        | Arguments: none
        |
        | Returns:
        |  status  -- system status code
        |  coef_ds -- coefficient data as struct defiend by typedef FLASH_COEFFICIENT_SECTION_104
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  coef = desc.FLASH_COEFFICIENT_SECTION_104()
        |  (status, coef) = Rx.get_coefficient_data()
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x01
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Information Query command
        """

        coef_ds = fs.FLASH_COEFFICIENT_SECTION_104()
        status = self.target.SWM_Diag_GetFlashData(0x0f0000, ctypes.sizeof(coef_ds), ctypes.byref(coef_ds))
        return(status, coef_ds)

    @trace
    @increase_timeout(3)
    @retry_datalog
    def set_coefficient_data(self, coef_ds):
        """
        Writes coefficient data to slave's flash memory

        | Arguments:
        |  coef_ds -- coefficient data as struct defiend by typedef FLASH_COEFFICIENT_SECTION_104
        |
        | Returns:
        |  status  -- system status code
        |  value   -- None
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import RxAPI
        |  Rx = RxAPI()
        |  coef = desc.FLASH_COEFFICIENT_SECTION_104()
        |  # load struct with coefficient data
        |  (status, null) = Rx.set_coefficient_data(coef)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x04
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Access command
        """
        assert ctypes.sizeof(coef_ds) == ctypes.sizeof(fs.FLASH_COEFFICIENT_SECTION_104)
        status = self.target.SWM_Diag_EraseFlashSector(0x0f)
        time.sleep(3) # It takes up to 3 seconds for flash to erase
        if(status == 0x01):
            status = self.target.SWM_Diag_SetFlashData(0x0f0000, ctypes.sizeof(fs.FLASH_COEFFICIENT_SECTION_104), ctypes.byref(coef_ds))

        return(status, None)

    @increase_timeout(3)
    @datalog
    def erase_coefficient_sector(self):
        status = self.target.SWM_Diag_EraseFlashSector(0x0f)
        time.sleep(3) # It takes up to 3 seconds for flash to erase
        return (status, None)

    @increase_timeout(10)
    def invoke_radio_cal_state(self, state, measurement=None):
        """Calibrate an Apollo based module.

        | Arguments:
        |  state -- enumerated value from RADIO_CAL_SM_STATE to be invoked
        |  measurement -- prior state's RF power measurement
        |
        | Returns:
        |  status  -- radio cal status code
        |  cal_sm_state -- enumerated state value from RADIO_CAL_STATUS

        """
        cal_sm_state = ctypes.c_ubyte(state)

        if(measurement == None):
            radio_cal_status = self.target.InvokeRadioCalState(ctypes.byref(cal_sm_state), None)
        else:
            cal_meas = ctypes.c_double(measurement)
            radio_cal_status = self.target.InvokeRadioCalState(ctypes.byref(cal_sm_state), ctypes.byref(cal_meas))
        return (radio_cal_status, cal_sm_state.value)

    @trace
    @retry_datalog
    def get_system_data(self):
        """
        Retreives system data from master's flash memory

        | Arguments: none
        |
        | Returns:
        |  status  -- system status code
        |  coef_ds -- coefficient data as struct defiend by typedef FLASH_COEFFICIENT_SECTION_104
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  coef = desc.FLASH_COEFFICIENT_SECTION_104()
        |  (status, coef) = Rx.get_coefficient_data()
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x60, Secondary: 0x01
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Information Query command
        """

        buffer = ctypes.c_ubyte(0xFFFF)
        status = self.target.SWM_Diag_GetFlashData(0x090000, 0xFFFF, ctypes.byref(buffer))
        return(status, buffer)

    @trace
    @increase_timeout(3)
    @retry_datalog
    def set_system_data(self, buffer):
        """
        Writes system data to master's flash memory

        | Arguments:
        |  buffer -- buffer of size 0xFFFF containing system data
        |
        | Returns:
        |  status  -- system status code
        |  value   -- None
        |
        | Example:
        |  import descriptors as desc
        |  from pysummit.devices import TxAPI
        |  Tx = TxAPI()
        |  (status, null) = Tx.set_coefficient_data(buffer)
        |  print "Status = ", status
        |
        | Opcodes:
        |  Main: 0x??, Secondary: 0x??
        |
        | See also:
        |  Summit SWM908 API Specification, Diagnostic Messages, Flash Access command
        """
        assert ctypes.sizeof(buffer) == 0xFFFF
        status = self.target.SWM_Diag_EraseFlashSector(0x09)
        time.sleep(3) # It takes up to 3 seconds for flash to erase
        if(status == 0x01):
            status = self.target.SWM_Diag_SetFlashData(0x090000, 0xFFFF, ctypes.byref(buffer))

        return(status, None)

class SystemStatusError(Exception):
    def __init__(self, cmd, status):
        self.cmd = cmd
        self.status = status
        logging.error("%s -- %s (0x%.2X)" % (cmd, dec.system_status.get(status, 'Unknown Error'), status))

if __name__ == '__main__':
#    import device
    import decoders as dec
    import utils
    logging.getLogger().setLevel(logging.DEBUG)
    logging.basicConfig()
    TX = TxAPI()
    (status, devid) = TX.get_devid()
    print "Master devid: 0x%X" % devid

    coms = [
        comport.ComPort('/dev/ttyUSB0'),
        comport.ComPort('/dev/ttyUSB1')
    ]
    try:
        RX = RxAPI(coms=coms)
    except SystemStatusError, info:
        print info.cmd, info.status
        print "ruh roh"

    if(len(RX) > 0):
        (status, coef_ds) = RX[0].get_coefficient_data()
        if(status == 0x01):
            coef_ds.write('out.txt')
        else:
            print self.decode_error_status(status)

        (status, null) = RX[0].set_coefficient_data(coef_ds)
        if(status == 0x01):
            print "Written!"
        else:
            print self.decode_error_status(status)
    else:
        print "Not enough RXs"

#
#    print "here"
#    print len(RX)
#    for rx in RX:
#        filename = rx['mac'] + '_mfg.txt'
#        filename = re.sub(':','',filename)
#        (status, null) = rx.mfg_dump(filename)
#
#    for rx in RX:
#        filename = rx['mac'] + '_coef.txt'
#        filename = re.sub(':','',filename)
#        (status, null) = rx.coefdump(filename)

#    print RX[0]['mac']
#    print RX.index("02:EA:4C:00:00:13")
#    print "02:EA:4C:00:00:13" in RX
#    print RX["02:EA:4C:00:00:13"]
#    print RX["02:EA:4C:00:00:13"]['fw_version']
#    print RX["02:EA:4C:00:00:13"]['speaker_type']

#    for rx in RX:
#        print rx['mac']

#    tp = testprofile.TestProfile('/home/tweaver/2sys.cfg')
#    tp = TX.load_profile('/home/tweaver/2sys.cfg')
#    if(tp):
#        (i2s_map, speaker_map) = TX.make_speaker_map(2, tp)
#        print i2s_map
#    else:
#        print "Couldn't get tp"
#    tp = TX.push_map_profile('/home/tweaver/sys.cfg')
#    for rx in RX:
#        exp_slot = tp.get("RX %s" % (rx['mac']), 'slot')
#        exp_slot = int(exp_slot,0)
#        (status, slot) = rx.rd(0x403040)
#        if(status == 0x01):
#            print "%s: exp:%d act:%d" % (rx['mac'], exp_slot, slot)
#            if(slot != exp_slot):
#                print "  ^^^ Invalid slot was set!"
#                break
#        else:
#            print self.decode_error_status(status)

    sys.exit(1)

#    RX.open()

#    funcs = [RX.get_speaker_operating_state,
#             RX.get_speaker_module_descriptor,
#             RX.get_speaker_descriptor,
#             RX.get_speaker_wisa_descriptor,
#             RX.get_speaker_amplifier_descriptor,
#             RX.get_speaker_global_coefficient_info,
#             RX.get_speaker_current_coefficient_info,
#             ]
#    for fn in funcs:
#        (status, buffer) = fn()
#        if(status != 0x01):
#            print "%s (0x%X)" % (dec.system_status.get(status, "UNKNOWN"), status)
#        else:
#            print buffer


#    print RX[0].get_fw_version()
#    for i in RX:
#        print i.id(), i['port'], i['mac'], i['xy'], i['speaker_type']

#    if('02:EA:4D:00:00:13' in RX):
#        print "Found 02:EA:4D:00:00:13 in RX"
#    else:
#        print "Didn't find 02:EA:4D:00:00:13 in RX"

#    print RX.index('02:EA:4D:00:00:10')
#    print RX.index('02:EA:4D:00:00:13')

#    print RX[1]['mac']

#    print "before"
#    print RX[0].netstat()
#    print "after"
#    (status, null) = TX.load_fw_from_file('Apollo_0186_Release.nvm')
#    (status, null) = RX[0].load_fw_from_file('Apollo_0186_Release.nvm')
#    print RX[0].check_active_image(0xfe, 0)
#    print RX[0].check_active_image(0xfe, 1)
#    print RX[0].get_our_mac()

#    (status, null) = TX.load_fw_from_file('Olympus_0186-01_Release.nvm')
#    (status, null) = TX.load_fw_from_file('Olympus_0186-01_Release.nvm')
#    print "%s (0x%X)" % (dec.system_status.get(status, "UNKNOWN"), status)
#    (status, null) = RX[0].load_fw_from_file('Apollo_0185_BUG_2750_14.nvm')
#    print "%s (0x%X)" % (dec.system_status.get(status, "UNKNOWN"), status)
#    with open('Apollo_0186_Release.nvm', 'rb') as f:
#        flashData = array.array('B', f.read(0x80))
#    cnt = len(flashData)
#    (status, null) = RX[0].load_firmware(0xfe, 1, 0, cnt, flashData)
#    print "%s (0x%X)" % (dec.system_status.get(status, "UNKNOWN"), status)

#    for rx in RX:
#        (status, null) = rx.load_fw_from_file('Apollo_0186_Release.nvm')
#        print "Status: %d" % status

    sys.exit(0)
#    coms = []
#    devices = device.Devices()
#    devices.load_config('2.0.yaml')
#    for device in devices:
#        coms.append(comport.ComPort(device.get('port', None)))

    import datalog
    D = datalog.DataLog('test.db')
    enable = signal('datalog_enable')
    flush = signal('datalog_flush')
    enable.send()
    for i in range(50):
        (status, null) = TX.echo(0)

#    (status, mac) = TX.get_our_mac()
#    print "%s" % mac
    flush.send()
#    print "0x%.2X" % val



#    coms = [
#        comport.ComPort('/dev/ttyUSB0'),
#        comport.ComPort('/dev/ttyUSB1')
#    ]

#    RX = RxAPI()
#    try:
#        RX.set_coms(coms)
#        print RX[0].scanning()
#        print RX[1].scanning()
#        TX.beacon(4500,99)
#        print ""
#        print RX[0].scanning()
#        print RX[1].scanning()
##        for slave in RX:
##            print "Scanning:", slave.scanning()
#    except SerialException as info:
#        print info
#        sys.exit(0)

#    loops = 5
#    for i in range(loops):
#    for data in [0x10, 0x20, 0x30, 0x40, 0x50]:
#        print RX[0].wr(0x400040, data, retries=5)
#        print RX[0].rd(0x400040, retries=5)

#    import datalog
#    D = datalog.DataLog('out.dat')
#    add_log = signal('datalog_add')
#    enable = signal('datalog_enable')
#    enable.send()
#    print TX.get_devid()
#    print RX[0].get_devid()
#    print RX[1].get_devid()
#    print D

#    print TX.test_deco()
#    print RX[0].test_deco()
#    print RX[1].test_deco()

#    (status, devid) = RX[0].get_devid(retries=10)
#    print "%d/%d: (0x%.2X)  0x%X" % (i+1, loops, status, devid)

#    loops = 5
#    for i in range(loops):
#        sys.stdout.write("%d/%d\r" % (i+1, loops))
#        sys.stdout.flush()
#        (status, reg) = RX[0].rd(0x400000)
#        if(status != 0x01):
#            print "\n0  (0x%.2X: %s) %X" % (status, dec.system_status.get(status, "UNKNOWN"), reg)
#        (status, reg) = RX[1].rd(0x400000)
#        if(status != 0x01):
#            print "\n1 (0x%.2X: %s) %X" % (status, dec.system_status.get(status, "UNKNOWN"), reg)
#
#    print ""

#    for i in coms:
#        i.connect()
#        print i.isOpen()

#    for i in RX:
#        for our_mac in i.get_our_mac():
#            (status, mac) = our_mac
#            print "%X" % status

#    if(not RX.verify_connections()):
#        for con in RX.con_stat:
#            print "port %s failed verification" % (con['status'])
#        sys.exit(1)
#
#    num_loops = 3
#    for i in range(num_loops):
#        print "%d/%d" % (i+1, num_loops)
#        for val in [0x10, 0x20, 0x30, 0x40, 0x50]:
#            for rx in RX:
#                (status, nul) = rx.wr(0x400040, val)
#                print " %d: wr (%X)" % (rx.port_index, status)
#                (status, reg) = rx.rd(0x400040)
#                print " %d: rd (%X): %X" % (rx.port_index, status, reg)
#    RX2 = RxAPI('/dev/ttyUSB1')
#    RX1.open()
#    print RX1.get_our_mac()
#    print RX1.get_devid()
#    RX1.close()

#    RX2.open()
#    print RX2.get_our_mac()
#    print RX2.get_devid()
#    RX2.close()

#    print TX.get_our_mac()
#    print TX.get_devid()
#    TX.close()


#    print TX.get_devid()
#    for i in range(1):
#        (status, data) = RX.get_devid()
#        print "RX: (%.2X) 0x%.2X" % (status, data)
#        (status, data) = TX.get_devid()
#        print "TX: (%.2X) 0x%.2X" % (status, data)

#    TX.close()
