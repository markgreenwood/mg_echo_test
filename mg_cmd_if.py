#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import imp
import math
from ConfigParser import NoOptionError
import glob
import traceback
import argparse
import time
import os
import atexit
import sys
from collections import OrderedDict
import ansistrm
import descriptors as desc
import comport
import suites
import logging
import re
import terminalsize
import decoders as dec
from devices import TxAPI
from devices import RxAPI
import utils
import datalog
import testprofile
from power_controller import PowerController
from __init__ import __version__, __swmapi_version__
import readline
import datetime
import flash_struct as fs
from termcolor import cprint
from wizard import Wizard
from argparse import RawTextHelpFormatter
if sys.platform.lower() == "darwin":
    readline.parse_and_bind("bind ^I rl_complete")
else:
    readline.parse_and_bind("tab: complete") # For Linux
#readline.set_completer_delims(' \t\n`~!@#$%^&*()-=+[{]}\\|;\'",<>/?') # Removed : from standard delims
readline.set_completer_delims(' \t\n`!@#%^&()=+[{]}\\|;\'",<>?') # Removed $*:/~- from standard delims

def myint(x): return int(x, 16)

class PST(datetime.tzinfo):
    def utcoffset(self, dt):
      return datetime.timedelta(hours=-7)

    def dst(self, dt):
        return datetime.timedelta(0)

class config(object):
    """Configuration decorator which adds function attributes used in command completion
    and command dispatch.
    dev_type: string
        app         - Application commands. i.e. exit, help
        dev_tx      - TX device specific commands. i.e. beacon, discover
        dev_rx      - RX device specific commands
        dev_all     - Common TX and RX device commands. i.e. rd, wr
        restr_tx    - Restricted TX device specific commands
        restr_rx    - Restricted RX device specific commands
        restr_all   - Restricted common commands
        restr_app   - Restricted application commands

    The choice_list is a list of lists where each list is a number of possible options
    for a given argument.
    choice_list: list of lists
        [[5500], [0,1,2,3,4,5]]
    """
    def __init__(self, dev_type, choice_list=[[]]):
        assert dev_type in ['app', 'dev_tx', 'dev_rx', 'dev_all', 'restr_tx', 'restr_rx', 'restr_all', 'restr_app']
        for inner_list in choice_list:
            for choice in inner_list:
                if(hasattr(choice, '__call__')):
                    pass
                else:
                    assert type(choice) == type(''), "choice_list must contain strings"
        self.dev_type = dev_type
        self.choice_list = choice_list

    def __call__(self, fn):
        fn.dev_type = self.dev_type
        fn.choice_list = self.choice_list
        return fn


def separator(txt):
    """Returns a pretty string separator with embedded text"""
    term_columns, sizey = terminalsize.get_terminal_size()
    out_str = '== {} {:=^{width}}'.format(txt,"",width=term_columns-21)
    return out_str

class RACompleter(object):
    def __init__(self, dev_type_fn, choice_fn, get_devs_fn,
                histfile=None):
        self.dev_type_fn = dev_type_fn
        self.choice_fn = choice_fn
        self.get_devs_fn = get_devs_fn
        self.__matches = []

        self.__all_cmds = {}
        self.__cmd = None

        self.__arg_index = 0
        if(histfile == None):
            histfile = "%s/.rahist" % utils.get_user_dir()
        self.init_history(histfile)

    # Initialize history from file.
    def init_history(self, histfile):
        if hasattr(readline, "read_history_file"):
            try:
                readline.read_history_file(histfile)
            except IOError:
                pass
            atexit.register(self.save_history, histfile)

    # Save history to file on exit
    def save_history(self, histfile):
        readline.write_history_file(histfile)

    def complete(self, text, state):
        """
        Commands can have the following forms:
        get_our_mac             - TX Command
        .get_our_mac            - All RX Command
        02:EA:00:00:01.get_our_mac  - Individual RX Command
        LF.get_our_mac          - Individual aliased RX Command
        """
        completion_type = readline.get_completion_type()
        match_types = {'SINGLE_MATCH': 0x09, 'MULTI_MATCH': 0x3F}

#        cmd_re = re.compile("^(\w*\.)?(\w+)")
#        cmd_re = re.compile("^(.*\.)(.*)")
        cmd_re = re.compile("^([a-z|A-Z|0-9|:]*?\.)?(\w+)\s*(.*)")
        dev_re = re.compile("^(.*\.)(.*)")
        orig_line = readline.get_line_buffer()
        begin = readline.get_begidx()
        end = readline.get_endidx()
        being_completed = orig_line[begin:end]
        words = orig_line.split()
        response = []

        # Commands w/wo/ devices
        try:
            cm = cmd_re.search(orig_line.strip())
            if(cm):
                self.__cmd =  cm.group(2)

            if(begin == 0):
                # Devices w/wo/ commands
                logging.debug("Device w/wo/ commands")
                dm = dev_re.search(orig_line.strip())
                if(dm):
                    ## Device command completer
                    logging.debug("Device command completer")
                    dev = dm.group(1)
                    logging.debug("  dev: %s" % dev)
                    devs = self.get_devs_fn()
                    if(dev == '.'):
                        logging.debug("  dev == '.'")
                        pass
                    elif(dev.strip('.') not in devs):
                        logging.debug("  dev.strip('.')")
                        return
                    self.__cmd = dm.group(2)
                    logging.debug("  cmd: %s" % self.__cmd)
                    cmd_list = self.dev_type_fn(state, ['dev_rx', 'dev_all'])
                    if(self.__cmd):
                        self.__matches = [s for s in cmd_list if s.startswith(self.__cmd)]
                        logging.debug("Matches: %r" % self.__matches)
                    else:
                        self.__matches = cmd_list
                        logging.debug("Matches: %r" % self.__matches)

                    if(self.__matches):
                        if(completion_type == match_types['MULTI_MATCH']):
#                        if(len(self.__matches) > 1):
                            response = self.__matches[state] + " "
                        else:
                            response = dev+self.__matches[state] + " "
                    logging.debug("  state: %d" % state)
                    logging.debug("  response: %r" % response)
                else:
                    ## Standard command completer
                    logging.debug("Standard command completer")
                    cmd_list = self.dev_type_fn(state, ['app', 'dev_tx', 'dev_all'])
                    if(being_completed):
                        self.__matches = [s for s in cmd_list if s.startswith(being_completed)]
                    else:
                        self.__matches = cmd_list[:]

                    if(self.__matches):
                        response = self.__matches[state] + " "
                        self.__cmd = response
            else:
                ## Argument completer
                logging.debug("Argument completer")
                logging.debug("words: %s" % words)
                logging.debug("begin: %s" % begin)
                logging.debug("end: %s" % end)
                if(begin == end):
                    self.__arg_index = len(words)-1
                else:
                    self.__arg_index = len(words)-2
                choices = self.choice_fn(self.__cmd, self.__arg_index, being_completed, state)
                if(being_completed):
                    self.__matches = [s for s in choices if s.startswith(being_completed)]
                else:
                    self.__matches = choices

                if(self.__matches):
                    self.__matches = [os.path.normpath(i) for i in self.__matches]
                    (path, file) = os.path.split(self.__matches[state])
                    if(os.path.isdir(self.__matches[state])):
                        response = self.__matches[state] + "/"
                    else:
                        response = self.__matches[state] + " "

            ## Device/Alias completer
            if(not response):
                logging.debug("Device/Alias completer")
                if(begin > 0):
                    return
                devs = self.get_devs_fn()
                if(not self.__matches):
                    if(being_completed):
                        self.__matches = [s for s in devs if s.startswith(being_completed)]
                    else:
                        self.__matches = devs[:]
                    response = self.__matches[state] + "."

        except Exception as info:
            self._report_traceback()
            # Without this catch any exceptions get squelched.
            logging.debug(info)

        # Return the completion
        return response

class RAConsole(object):
    def __init__(self, logging_level,
            interactive=True,
            tx_interface=None,
            tx_param1=None,
            tx_param2=None,
            rx_uart_ports=None,
            dut_pwr=False):
        self.__interactive = interactive
        self.__rx_uart_ports = rx_uart_ports
        self.__dut_pwr = dut_pwr
        self.__logger = logging.getLogger(__name__)
        self.__logger.setLevel(logging_level)
        self.cmd_re = re.compile("^([a-z|A-Z|0-9|:]*?\.)?(\w+)\s*(.*)")

        self.pi_bsp = None
        try:
            from pysummit.bsp.pi_bsp import PiBSP
            self.pi_bsp = PiBSP()
            if self.__dut_pwr:
                print "{} Power on DUT".format(
                    "[FAIL]" if self.pi_bsp.dut_pwr(enable=True) else "[ OK ]")
                time.sleep(3) #  boot time
        except:
            print "not a Raspberry Pi platform!"

        if tx_interface == 'usb' or tx_interface == 'i2c':
            self.__tx_dev = TxAPI(com=tx_interface,
                                  param1=tx_param1,
                                  param2=tx_param2,
                                  bsp=self.pi_bsp)
        elif tx_interface == 'uart':
            if tx_param1 == None:
                print "\n<<< Command line error: --interface uart requires serial port or URL >>>\n"
                raise Exception
            else:
                self.__tx_dev = TxAPI(com=comport.ComPort(tx_param1, timeout=2),
                                      bsp=self.pi_bsp)
        else:
            print "\n<<< Specified Tx interface invalid >>>\n"
            raise Exception

        self.__rx_devs = RxAPI()
#        if self.__rx_uart_ports is None:
#            self.__rx_devs = RxAPI(coms=comport.ComPort.get_open_ports())
#        else:
#            self.__rx_devs = RxAPI(coms=comport.ComPort.get_coms(self.__rx_uart_ports))
        self.__files = []
#        self.__datalog = datalog.DataLog()
        self.__test_profile = testprofile.TestProfile()
        self.__power_controller = None
        self.__current_choice_list = []
        self.__current_command_list = []
        self.__exit_app = False
        self.__trace = False

        print "Initializing..."
#        if(self.__interactive):
#            self.collect_devs()
        RC = RACompleter(
            dev_type_fn=self._get_cmds_of_type,
            choice_fn=self._get_choices,
            get_devs_fn=self._get_devs)
        readline.set_completer(RC.complete)

    def cmdloop(self):
        print "== Summit Command Monitor v%s (SWMAPI v%s) ==" % (__version__, __swmapi_version__)
        while True:
            self.__tx_dev.set_trace(self.__trace)
            try:
                line = raw_input("ra:z%d> " % self.__tx_dev['zone'])
                self._dispatch(line.strip())
                if(self.__exit_app == True):
                    break
            except KeyboardInterrupt as info:
                print ""
            except EOFError as info: # CTRL-d pressed
                print ""
            except:
                raise
        print "Exiting..."
        self.cleanup()

    def cleanup(self):
        self.__rx_devs.close_coms()
        if self.__dut_pwr and (self.pi_bsp is not None):
            print "{} Power off DUT".format(
                "[FAIL]" if self.pi_bsp.dut_pwr(enable=False) else "[ OK ]")

    def _report_invalid_cmd(self):
        self.__logger.error("-- invalid command")
        print("type 'help' for a list of valid commands")

    def _report_traceback(self, override_debug_check=False):
        if (override_debug_check == True) or (self.__test_profile.getboolean('SETTINGS', 'debug') == True):
            for line in traceback.format_exc().splitlines():
                self.__logger.error(line)

    def _dispatch(self, cmd_line):
        """Determine the device on which to run the command"""
        self.__logger.debug(cmd_line)
        self.__device = None
        if(cmd_line == ''):
            return
        cm = self.cmd_re.search(cmd_line)
        if(cm):
            dev = cm.group(1)
            cmd = cm.group(2)
            args = cm.group(3)
            self.__logger.debug("dev: %s" % dev)
            self.__logger.debug("cmd: %s" % cmd)
            self.__logger.debug("args: %s" % args)
            if(hasattr(self, cmd)):
                fn = getattr(self, cmd)
                try:
                    if(hasattr(fn, 'dev_type')):
                        dev_type = fn.dev_type
                        if(dev is None):
                            if (dev_type in ['app', 'restr_app']):
                                self.__device = None
                            elif (dev_type in ['dev_tx', 'dev_all', 'restr_tx', 'restr_all']):
                                self.__device = self.__tx_dev
                            else:
                                self._report_invalid_cmd()
                                return

                            ret = fn(*args.split())
                            if(ret):
                                print("%s" % (ret))

                        else:
                            if (dev_type in ['dev_rx', 'dev_all', 'restr_rx', 'restr_all']):
                                if(dev == '.'):
                                    for rx in self.__rx_devs:
                                        self.__device = rx
                                        ret = fn(*args.split())
                                        if(ret):
                                            print("%s (%s)" % (ret, rx['mac']))
                                elif(dev.strip('.') in self.__rx_devs):
                                    self.__device = self.__rx_devs[dev.strip('.')]
                                    ret = fn(*args.split())
                                    if(ret):
                                        print("%s" % (ret))
                                else:
                                    self.__logger.error("%s is an invalid device" % dev.strip('.'))
                            else:
                                self._report_invalid_cmd()
                    else:
                        self._report_invalid_cmd()
                except TypeError as info: # Wrong number of arguments passed to method
                    self._report_traceback()
                    print(self.help(cmd))
                except ValueError as info: # Wrong type of arguments passed to method
                    self._report_traceback()
                    print(self.help(cmd))
                except KeyboardInterrupt as info: # CTRL-c pressed
                    self.__logger.debug(info)
                    print("Aborting...")
                except Exception as info:
                    self.__logger.critical("-- Blurg! - Application Error --")
                    traceback.print_exc()
            else:
                self.__logger.debug("dev: %s cmd: %s args: %s" % (dev, cmd, args))
                self._report_invalid_cmd()
        else:
            self.__logger.debug("no command match: %s" % (cmd_line))
            self._report_invalid_cmd()

    def _get_devs(self):
        dev_list = [dev['mac'] for dev in self.__rx_devs]
        return dev_list

    def _get_cmds_of_type(self, state, types=None):
        """Return a list of commands given a device type as specified in the
        config decorator."""
        if(state == 0):
            self.__current_command_list = []
            self.__logger.debug("_get_cmds_of_type")
            if(not types): # Return commands of all types
                types = ['app', 'dev_all', 'dev_rx', 'dev_tx']
            for cmd in dir(self):
                fn = getattr(self, cmd)
                if(hasattr(fn, 'dev_type')):
                    if(fn.dev_type in types):
                        self.__current_command_list.append(cmd)
        return self.__current_command_list


    def _get_choices(self, cmd, arg_index, being_completed, state):
        """Return a list of arguments given a command name and an argument index."""
        if(state == 0):
            self.__current_choice_list = []
            self.__logger.debug("_get_choices")
            self.__logger.debug("  cmd: %s" % cmd)
            self.__logger.debug("  arg_index: %d" % arg_index)
            self.__logger.debug("  being_completed: %s" % being_completed)
            self.__logger.debug("  state: %d" % state)
            self.__logger.debug("hasattr(self, %r)" % cmd)
            if(hasattr(self, cmd)):
                self.__logger.debug("getattr(self, %r)" % cmd)
                fn = getattr(self, cmd)
                self.__logger.debug("fn = %r" % fn)
                self.__logger.debug("hasattr(%r, %s)" % (cmd, "'choice_list'"))
                if(hasattr(fn, 'choice_list')):
                    if(arg_index < len(fn.choice_list)):
                        self.__logger.debug("Here: 0")
                        self.__logger.debug("fn.choice_list: %r" % fn.choice_list)
                        self.__logger.debug("fn.choice_list[arg_index][0] = %r" % fn.choice_list[arg_index])
                        if(hasattr(fn.choice_list[arg_index][0], '__call__')):
                            self.__logger.debug("Here: 1")
                            func = fn.choice_list[arg_index][0]
                            self.__logger.debug("func: %r" % func)
                            try:
                                self.__current_choice_list = func(fn.__self__, being_completed, state)
                            except Exception as info:
                                self._report_traceback()
                        else:
                            self.__logger.debug("choice_list[%d]" % arg_index)
                            self.__logger.debug("choice_list: %r" % fn.choice_list[arg_index])
                            self.__current_choice_list = fn.choice_list[arg_index]
#        return ret
        return self.__current_choice_list

    def _order_list(self, seq, idfun=None):
        """Uniquify and reverse order a list of integers."""
        if idfun is None:
            def idfun(x): return x
        seen = {}
        result = []
        for item in seq:
            marker = idfun(item)
            if marker in seen: continue
            seen[marker] = 1
            result.append(item)
        result.sort(reverse=True)
        return result

#==============================================================================
# Application Commands
#==============================================================================
    def _trim_docstr(self, docstring):
        """Trim docstrings

        Taken directly from PEP 257:
        http://www.python.org/dev/peps/pep-0257/

        """
        if not docstring:
            return ''
        # Convert tabs to spaces (following the normal Python rules)
        # and split into a list of lines:
        lines = docstring.expandtabs().splitlines()
        # Determine minimum indentation (first line doesn't count):
        indent = sys.maxint
        for line in lines[1:]:
            stripped = line.lstrip()
            if stripped:
                indent = min(indent, len(line) - len(stripped))
        # Remove indentation (first line is special):
        trimmed = [lines[0].strip()]
        if indent < sys.maxint:
            for line in lines[1:]:
                trimmed.append(line[indent:].rstrip())
        # Strip off trailing and leading blank lines:
        while trimmed and not trimmed[-1]:
            trimmed.pop()
        while trimmed and not trimmed[0]:
            trimmed.pop(0)
        # Return a single string:
        return '\n'.join(trimmed)

#== Directory and file methods ================================================
    def _dirs(self, dir, state, only_basename=False):
        self.__logger.debug("_dirs: %s %s" % (dir, state))
#            dir = os.path.normpath(dir)
#            dir = dir.strip()
        if(dir == ''):
            dir = './*'
        if(dir == '.'):
            dir = './*'
        elif(dir == '..'):
            dir = '../*'
        elif(os.path.isdir(dir)):
            dir = dir + '/*'
        elif(os.path.isfile(dir)):
            dir = dir
        elif(not only_basename):
            dir = dir + '*'

        if(only_basename):
            files = glob.glob(dir)
            files = [os.path.basename(f) for f in files]
        else:
            files = glob.glob(dir)

        files = ["%s/" % f if os.path.isdir(f) else "%s" % f for f in files]
        files.sort(key=str.lower)
        self.__files = files
        return self.__files

    @config('dev_rx', [['enable', 'disable']])
    def logging(self, enable=None):
        """Start/Stop serial port logging"""
        if((enable == 'enable') or (enable == '1')):
            self.__device['logging'] = True
            self.__device.start_logging()
            return "Enabled "
        elif((enable == 'disable') or (enable == '0')):
            self.__device.stop_logging()
            self.__device['logging'] = False
            return "Disabled"
        else:
            if(self.__device['logging']):
                return "Enabled "
            else:
                return "Disabled"

    @config('app', [[_dirs]])
    def ls(self, dir=''):
        """List directory contents."""
        width = 1
        files = self._dirs(dir, 0, only_basename=True)
        term_columns, sizey = terminalsize.get_terminal_size()
        if(files):
            width = len(max(files, key=len)) + 1
        width = max(width, 1)
        words_per_line = (term_columns-1)/width
        self.__logger.debug("term_columns: %d" % term_columns)
        self.__logger.debug("width: %d" % width)
        self.__logger.debug("words_per_line: %d" % words_per_line)

        word_count = 1
        out_str = ""
        if(files):
            for index in range(len(files)):
                if(os.path.isdir(files[index])):
                    out_str += '{0:{width}}'.format(files[index], width=width)
                else:
                    out_str += '{0:{width}}'.format(os.path.basename(files[index]), width=width)
                if((word_count % words_per_line) or (index == len(files)-1)):
                    pass
                else:
                    out_str += "\n"
                word_count += 1
            print out_str
        else:
            print "ls: %s: no such file or directory" % dir

    @config('app', [[_dirs]])
    def cd(self, dir=utils.get_user_dir()):
        """Change directory"""
        if(os.path.isdir(dir)):
            os.chdir(dir)

    @config('app')
    def pwd(self):
        """Print the current directory name."""
        return os.getcwd()

#== Help ======================================================================
    @config('app')
    def help(self, cmd=None):
        """Print out command help.

        usage: help [command]

        """
        if(cmd):
            if(hasattr(self, cmd)):
                fn = getattr(self, cmd)
                return self._trim_docstr(getattr(fn, '__doc__'))
        else:
            cmd_types = OrderedDict([
                ('app', 'Application Commands'),
                ('dev_all', 'Shared Commands'),
                ('dev_tx', 'TX Commands'),
                ('dev_rx', 'RX Commands'),
            ])
            max_title_len = max([len(cmd_types[i]) for i in cmd_types])
            self.__logger.debug("max_title_len: %d" % max_title_len)
            max_cmd_len = max(map(len, self._get_cmds_of_type(state=0))) + 1
            self.__logger.debug("max_cmd_len: %d" % max_cmd_len)
            for cmd_type in cmd_types.keys():
                cmds = self._get_cmds_of_type(state=0, types=cmd_type)
                if(cmds):
                    title = cmd_types[cmd_type]
                    print "\n%s" % title
                    print "="*max_title_len
                    for cmd_index in range(len(cmds)):
                        if(hasattr(self, cmds[cmd_index])):
                            fn = getattr(self, cmds[cmd_index])
                            doc_string = getattr(fn, '__doc__')
                            if(doc_string):
                                lines = doc_string.split('\n')
                            else:
                                lines = ['-- No help available --']
                            cmd = cmds[cmd_index]
                            print "  {:<{max_cmd_len}} {:}".format(cmd, lines[0], max_cmd_len=max_cmd_len)

    @config('app')
    def version(self):
        """Print out the version numbers"""
        print "SWMAPI v%s" % __swmapi_version__
        print "Ra v%s" % __version__

    @config('app')
    def exit(self):
        """Exit the application."""
#        return -1
        self.__exit_app = True
#        sys.exit(0)

    @config('app')
    def quit(self):
        """Exit the application."""
#        return -1
        self.__exit_app = True
#        sys.exit(0)

    @config('app')
    def collect_master_info(self, logging_enable=True):
        """(Re)Gather information from the master"""
        self.__tx_dev.collect_master_info()
        self._master_info_basic()

    @config('app', [['disable_logging']])
    def collect_devs(self, logging_enable=True):
        """Find all devices connected to serial ports."""
        coms = []
        if(logging_enable != True):
            logging_enable = False

        if self.__rx_uart_ports is None:
            if self.__test_profile.has_section('NETWORK_SERIAL'):
                if self.__test_profile.has_option('NETWORK_SERIAL', 'sockets'):
                    for socket in self.__test_profile.get('NETWORK_SERIAL', 'sockets').split():
                        coms.append(comport.ComPort(socket))

            coms.extend(comport.ComPort.get_coms())
        else:
            for port in self.__rx_uart_ports:
                coms.append(comport.ComPort(port))

        self.__rx_devs.set_coms(
            coms,
            logging_enable=logging_enable)

        self._print_devs()

    @config('app')
    def devs(self):
        """Print out the currently connected serial devices."""
        self._print_devs()

    def _print_devs(self):
        """Print out the currently connected serial devices."""
        if(len(self.__rx_devs) > 0):
            for dev in self.__rx_devs:
                print("%s - %s - %s - %s" % (
                    dev['mac'],
                    dev['fw_version'],
                    dev['port'],
                    "Logging: %s" % ("Enabled" if dev['logging'] else "Disabled"),
                    )
                )

    def _settings(self, dir, state, only_basename=False):
        return self.__test_profile.options('SETTINGS')

    @config('app', [[_settings]])
    def set(self, key=None, value=None):
        """Set local variables.

        usage: set [<keyword> <value>]

        set with no options returns all current settings
        """
        options = self.__test_profile.options('SETTINGS')
        if(key is None):
            longest = len(max(options, key=len))
            for option in options:
                print("{:{width}} {}".format(
                    option,
                    self.__test_profile.get('SETTINGS', option),
                    width=longest))
            return

        if(key in options):
            if(value is not None):
                self.__test_profile.set('SETTINGS', key, value)
                self.__test_profile.validate()
            else:
                self.__logger.error("missing value")
                self.__logger.error("  set [<keyword> <value>]")
                return
        else:
            self.__logger.error("invalid keyword: %s" % key)
            self.__logger.error("Valid keywords are:")
            for option in options:
                print("  %s" % option)

    @config('app')
    def sleep(self, sec):
        """Sleep for a period of time.

        usage: sleep <seconds>

        """
        sleep_time = float(sec)
        integer_time = int(sleep_time)
        remainder_time = sleep_time - integer_time

        if(sleep_time > 1):
            for i in range(integer_time):
                sys.stdout.write('%d\r' % (i+1))
                sys.stdout.flush()
                time.sleep(1)
            time.sleep(remainder_time)
        else:
            time.sleep(sleep_time)

    @config('app', [['disable','enable']])
    def trace(self, state='blank'):
        """Display SummitAPI calls and opcodes used by Ra commands.

        usage: trace <disable/enable>
        """
        if state == 'blank':
            print '%s' % 'enabled' if self.__trace == True else 'disabled'
        elif state in ['disable', 'enable']:
            self.__trace = (state == 'enable')
        else:
            print(self.help('trace'))

    @config('app', [[_dirs]])
    def load_test_profile(self, filename):
        """Load a test test profile."""
        if(os.path.exists(filename)):
            self.__test_profile = testprofile.TestProfile()
            self.__test_profile.readfp(open(filename))
            self.__test_profile.validate()
            self._verify_power_profile()
        else:
            print "%s doesn't exist" % filename
            return

    def _verify_test_profile(self):
        """Verifies that the TX device and RX devices defined in the test
        profile are present in the current system"""
        verify_ok = True
        print("[Verifying test profile...]")
        profile_tx_mac = None
        profile_rx_macs = []
        print("Checking TX and RX device MACs...")
        for section in self.__test_profile.sections():
            if(re.search('^[T|R]X', section)):
                (typ, mac) = section.split()
                if(typ == 'RX'):
                    profile_rx_macs.append(mac)
                elif(typ == 'TX'):
                    profile_tx_mac = mac

        if(self.__tx_dev['mac'] == profile_tx_mac):
            print("[ OK ] TX Device MAC")
        else:
            self.__logger.error("[FAIL] TX Device MAC")
            print("  Connected TX MAC: %s" % (self.__tx_dev['mac']))
            print("  Profile TX MAC  : %s" % (profile_tx_mac))
            verify_ok = False

        connected_rx_macs = []
        for rx in self.__rx_devs:
            connected_rx_macs.append(rx['mac'])

        spm = set(profile_rx_macs)
        scm = set(connected_rx_macs)
        if((len(spm - scm) == 0) and (len(scm - spm) == 0)):
            print("[ OK ] RX Device MACs")
        else:
            self.__logger.error("[FAIL] RX Device MACs")
            both = set(profile_rx_macs+connected_rx_macs)
            hdr_str = "{:<17}  {:<17}".format("Profile", "Connected")
            print(hdr_str)
            print("{:=^{width}}".format('', width=len(hdr_str)))
            for i in both:
                print("{:<17}  {:<17}".format(i if i in profile_rx_macs else '', i if i in connected_rx_macs else ''))
            verify_ok = False

        if(verify_ok):
            return True
        else:
            return False

    def _verify_power_profile(self):
        # Check for power controller
        verify_ok = True
        if(self.__test_profile.has_section("POWER")):
            print("[Verifying power profile...]")
            if(not self.__test_profile.has_option("POWER", "hostname")):
                self.__logger.error("You must provide a 'hostname' in the [POWER] section")
            if(not self.__test_profile.has_option("POWER", "userid")):
                self.__logger.error("You must provide a 'userid' in the [POWER] section")
                verify_ok = False
            if(not self.__test_profile.has_option("POWER", "password")):
                self.__logger.error("You must provide a 'password' in the [POWER] section")
                verify_ok = False
            if(not self.__test_profile.has_option("POWER", "outlets")):
                self.__logger.error("You must provide an 'outlets' field in the [POWER] section. This defines the power outlets that will be cycled by the test.")
                verify_ok = False
            else:
                outlets = self.__test_profile.get("POWER", "outlets")
                outlets = outlets.split()
                outlets = map(int, outlets)
                self.__power_controller = PowerController(
                    self.__test_profile.get("POWER", "hostname"),
                    self.__test_profile.get("POWER", "userid"),
                    self.__test_profile.get("POWER", "password"),
                    outlets,
                )
#        else:
#            self.__logger.warning("No power profile available")

        if(verify_ok):
            return True
        else:
            return False

    @config('restr_app', [["on", "off"]])
    def power(self, on_off=None, *args):
        """Enable/disable web power outlets if they are available.

        usage: power [on|off [1-8]]

        A test profile that contains a [POWER] section must be loaded before
        this command will be useful.

        Example:

        Turn on all outlets defined in the test profile
            ra> power on

        or off:

            ra> power off

        Turn on specific outlets
            ra> power on 3 5 7

        """
        if not self.__test_profile.has_section("POWER"):
            print "The test profile doesn't contain a [POWER] section."
            return

        if not self.__test_profile.has_option("POWER", "outlets"):
            print "No power profile is defined"
            return

        if on_off:
            outlets = map(int, args)
            if on_off.upper() == "ON":
                self.__power_controller.power("ON", outlets)
            elif on_off.upper() == "OFF":
                self.__power_controller.power("OFF", outlets)

        outlet_status = self.__power_controller.status()
        if(outlet_status):
            longest_string = max(max([map(len, b) for b in outlet_status]))
            for status in outlet_status:
                print "{:}: {:{width}} - {}".format(status[0], status[1], status[2], width=longest_string)


    @config('app', [[_dirs]])
#    def script(self, filename, *args):
    def script(self, *args):
        """Read and execute an external Python script.

        usage: script [options] <filename.py> [args]

        The script must have a 'main' function with the following signature:
            main(TX, RX, test_profile, power_controller, args)
        where TX is an instance of the TxAPI and RX is an instance of the
        RxAPI.

        """
        parser = argparse.ArgumentParser(
            prog='script',
            description="Execute external scripts.")
        parser.add_argument('filename')
        parser.add_argument('-c', action="store_true", dest='command_file')
        parser.add_argument('args', nargs=argparse.REMAINDER)
        try:
            args = parser.parse_args(args)
        except SystemExit as info:
            return

        if(not os.path.exists(args.filename)):
            print "%s doesn't exist" % args.filename
            return

        if args.command_file:
            print "Parsing command file '%s'" % args.filename
            with open(args.filename, 'r') as f:
                commands = f.readlines()
            # Strip out all newlines
            commands = [f.strip() for f in commands]
        else:
            commands = [args.filename]

        # List of dicts to hold test info and results
        results = []

        # Loop over all scripts to be run
        full_start_time = time.time()
        for f in commands:
            file_and_args = f.split()
            if(len(file_and_args) > 0):
                filename = file_and_args[0]
                if(not os.path.exists(filename)):
                    continue

            if args.command_file:
                args.args = file_and_args[1:]
                print "== %s %s" % (filename, " ".join(args.args))

            try:
                sys.dont_write_bytecode = True
                status = "INCOMPLETE"
                dirname = os.path.dirname(filename)
                basename = os.path.basename(filename)
                (module_name, ext) = os.path.splitext(basename)

                (mod_file, mod_path, mod_desc) = imp.find_module(
                    module_name, [dirname])
                (name, ext) = os.path.splitext(filename)
                # Remove modules that are already loaded
                sys.modules.pop(name, None)
                mod = imp.load_module(name, mod_file, mod_path, mod_desc)
#                if(hasattr(mod, "__version__")):
#                    print mod.__version__
                self._verify_power_profile()
                start_time = time.time()
                status = mod.main(
                            self.__tx_dev,
                            self.__rx_devs,
                            self.__test_profile,
                            self.__power_controller,
                            args.args)
            except:
                self._report_traceback(override_debug_check=True)
            finally:
                end_time = time.time()
                if(mod_file):
                    mod_file.close()
                sys.dont_write_bytecode = False

                # Stats
                results.append(dict({
                    "status": "%s" % (status),
                    "name": "%s %s" % (filename, " ".join(args.args)),
                    "elapsed_time": "%s" % (self.delta_to_hms(end_time-start_time)),
#                    "version": "%s" % (mod.__version__),
                    "version": "%s" % (getattr(mod, "__version__", "unknown")),
                }))

        full_end_time = time.time()
        if args.command_file:
            max_name_len = max([len(x['name']) for x in results])
            max_status_len = max([len(x['status']) for x in results])+2
            print ""
            print "Results"
            print "=============================================================================="
            for test in results:
#                print "[{status:<{stat_width}}] {name:<{width}} (v{version:} - {elapsed_time})".format(
                print "{status:<{stat_width}} {name:<{width}} (v{version:} - {elapsed_time})".format(
                    width=max_name_len,
                    stat_width=max_status_len,
                    status="[%s]" % test.get('status', "unknown"),
                    name=test.get('name', "unknown"),
                    version=test.get('version', "unknown"),
                    elapsed_time=test.get('elapsed_time', "unknown")
                    )

            # Count passed tests
            pass_fail = [x['status'].upper() == "PASS" for x in results]
            total_tests = len(pass_fail)
            total_pass = pass_fail.count(True)
            total_fail = total_tests - total_pass
            total_elapsed_time = full_end_time - full_start_time
            print ""
            print "Run Time: %s" % self.delta_to_hms(total_elapsed_time)
            print "Passed {:3}/{} ({:.2%})".format(total_pass, total_tests,
                float(total_pass)/float(total_tests))
            print "Failed {:3}/{} ({:.2%})".format(total_fail, total_tests,
                float(total_fail)/float(total_tests))

    def delta_to_hms(self, seconds):
        """Converts seconds to a formatted hours:minutes:seconds"""
        seconds_mod = seconds%60.
        hours   = seconds/3600.
        temp_minutes = hours*60.
        minutes = temp_minutes%60.
#        return "%.2dh %.2dm %.2ds" % (hours, minutes, seconds)
        return "%.2dm %.2fs" % (temp_minutes, seconds_mod)

    def _check_db_cridentials(self):
        """
        Returns True if the test profile contains the requisite items in the
        DATABASE section.

        """
        OK = True
        if(self.__test_profile.has_section('DATABASE')):
            if(not self.__test_profile.has_option('DATABASE', 'username')):
                self.__logger.error("The test profile requires a 'username' entry in the [DATABASE] section.")
                OK = False
            if(not self.__test_profile.has_option('DATABASE', 'password')):
                self.__logger.error("The test profile requires a 'password' entry in the [DATABASE] section.")
                OK = False
            if(not self.__test_profile.has_option('DATABASE', 'hostname')):
                self.__logger.error("The test profile requires a 'hostname' entry in the [DATABASE] section.")
                OK = False
            if(not self.__test_profile.has_option('DATABASE', 'database_name')):
                self.__logger.error("The test profile requires a 'database_name' entry in the [DATABASE] section.")
                OK = False
        else:
            self.__logger.error("The test profile requires a [DATABASE] section")
            OK = False

        return OK

    @config('restr_app', [suites.__all__])
    def run(self, test, *args):
        self.__datalog = None
        db_type = None
        """Run a regression tests."""
        print("[Verifying test: %s...]" % test)
        if(test not in suites.__all__):
            self.__logger.error("Invalid test")
            return

        # Reload the test module in case it's changed since starting the
        # console
        self.__logger.debug("Reloading 'pysummit.suites.%s' module" % test)
        reload(sys.modules.get('pysummit.suites.%s' % test, ''))
        funcs = []
        mod = getattr(suites, test)
        main_func = getattr(mod, 'main')

        # Check for the parse_args function. Use it to get iterations if
        # possible. Iterations are needed for setting up a new database run.
        if(hasattr(mod, 'parse_args')):
            parse_args_func = getattr(mod, 'parse_args')
            the_args = parse_args_func(args)
            if(the_args):
                if(the_args.iterations):
                    iterations = the_args.iterations
                else:
                    iterations = 1
            else:
                return
        else:
            self.__logger.error("The regression test is missing the 'parse_args' function.")
            self.__logger.error("This function is now required.")
            return

        if(hasattr(mod, 'verify')):
            self.__logger.debug("%s has a verify function" % test)
            verify_func = getattr(mod, 'verify')
        else:
            self.__logger.debug("%s has no verify function" % test)
            verify_func = None

        try:
            if(self.__test_profile.loaded):
                if(not self._verify_power_profile()):
                    return
                else:
                    # Cycle power to slave outlets
                    if(self.__power_controller):
                        if(not self.__power_controller.online()):
                            print "The power controller is offline"
                            return
                        else:
                            print "Power off..."
                            self.__power_controller.off()
                            time.sleep(3)
                            print "Power on..."
                            self.__power_controller.on()
                            print "Wait for reboot..."
                            time.sleep(5)
                            if(self.__test_profile.getboolean("SETTINGS", "serial_log_regressions")):
                                self.collect_devs()
                            else:
                                self.collect_devs('disable_logging')

                if(not self._verify_test_profile()):
                    return
            else:
                self.__logger.error("No test profile has been loaded")
#                print("Load one with load_test_profile, or create one with make_test_profile")
                self.__logger.error("Load one with load_test_profile")
                return

            if(verify_func):
                if(not verify_func(
                        self.__tx_dev,
                        self.__rx_devs,
                        self.__test_profile,
                        self.__power_controller
                        )
                ):
                    self.__logger.debug("%s verification failed." % test)
                    return

            if(self.__test_profile.getboolean('SETTINGS', 'db')):
                self.__datalog = datalog.DataLog()

                if(self.__test_profile.has_section('DATABASE')):
                    if(self.__test_profile.has_option('DATABASE', 'type')):
                        db_type = self.__test_profile.get('DATABASE', 'type')

                try:
                    if(db_type == 'sqlite'):
                        self.__datalog.new_run(
                            test,
                            iterations,
                            'user',
                            self.__tx_dev,
                            self.__rx_devs,
                            __version__,
                            db_type='sqlite')
                    else:
                        if(self._check_db_cridentials()):
                            username = self.__test_profile.get('DATABASE', 'username')
                            password = self.__test_profile.get('DATABASE', 'password')
                            hostname = self.__test_profile.get('DATABASE', 'hostname')
                            database_name = self.__test_profile.get('DATABASE', 'database_name')
                            self.__datalog.new_run(
                                test,
                                iterations,
                                'user',
                                self.__tx_dev,
                                self.__rx_devs,
                                __version__,
                                db_type='postgres',
                                username=username,
                                password=password,
                                hostname=hostname,
                                database_name=database_name)
                        else:
                            return
                except Exception as info:
                    self.__logger.error(info)
                    return
            else:
                self.__logger.warning("Database logging is disabled! Enable with 'set db true'")
                self.__logger.warning("or update the test profile.")
                time.sleep(2)

            self.__logger.debug("__tx_dev: %r" % self.__tx_dev)
            self.__logger.debug("__rx_dev: %r" % self.__rx_devs)
            self.__logger.debug("iterations: %r" % iterations)
            self.__logger.debug("__test_profile: %r" % self.__test_profile)
            main_func(
                self.__tx_dev,
                self.__rx_devs,
                self.__test_profile,
                self.__power_controller,
                the_args
            )
            if self.__datalog is not None:
                print "Cleaning up..."
                self.__datalog.complete_run()
        except Exception, info:
            self.__logger.critical("Test system failure")
#            self.__logger.error(info)
            self.__logger.error(traceback.print_exc())
        finally:
            if self.__datalog is not None:
                self.__datalog.disable()
            # Turn off power to slave outlets
            self.__logger.debug("Shutting off all power outlets")
            if(self.__power_controller is not None):
                self.__power_controller.off()
                # Give the modules a little time to power down
                time.sleep(2)
                self.collect_devs()

    @config('restr_app')
    def results(self):
        """Prints out any test case results."""
        if self.__datalog is not None:
            print self.__datalog.results()

#==============================================================================
# TX Device Commands
#==============================================================================
    @config('dev_tx', [['5500'], map(str, range(34)+[99])])
    def beacon(self, period, channel):
        """Beacon on a given channel for a set period of time.

        usage: beacon <period> <channel>

        """
        (status, nul) = self.__device.beacon(int(period,0), int(channel,0))
        self.__device.decode_error_status(status, cmd='beacon(%s, %s)' % (period, channel), print_on_error=True)

    @config('dev_tx', [['in', 'out', 'usb'], ['48', '96', '192']])
    def i2s_clocks(self, direction=None, clk_rate='48'):
        """Set I2S clock direction and rate.

        usage: i2s_clocks [in|usb|out [48|96|192]]

        in:
            I2S clock lines are tri-stated and must be driven externally to the
            Summit TX device.

        usb:
            Set up I2S clocks for compatibility with USB TX devices.

        out 48 (default):
            I2S clocks (in KHz) are generated by the Summit TX device.

        If no arguments are provided the current state of the I2S clocks is
        printed.

        """
        if(not direction):
            in_out = {0: "- tri-state", 1: "- driven"}
            (status, mos) = self.__tx_dev.get_master_operating_state()
            mas = mos.audioInputSetup.audioClockSetup.audioSetup
            mcs = mos.audioInputSetup.audioClockSetup
            print "{:7} {}".format("Source:",
                dec.audio_source.get(mcs.audioSource, 'unknown'))

            print "{:7} {:10} {}".format("LRCLK:",
                dec.sample_rates.get(mos.networkAudioClockRate, "unknown"),
                in_out.get(mas.driveClks, "unknown"))

            print "{:7} {:10} {}".format("SCLK:",
                dec.sclk_freq.get(mas.sclkFrequency, 'unknown'),
                in_out.get(mas.driveClks, "unknown"))

            print "{:7} {:10} {}".format("MCLK:",
                dec.mclk_freq.get(mas.mclkFrequency, 'unknown'),
                in_out.get(mas.mclkOutputEnable, "unknown"))
        else:
            clks = desc.AUDIO_CLOCK_SETUP()
            clks.audioSource = 1 # I2S
            if(direction == 'in'):
                clks.audioSetup.driveClks = 0
                clks.audioSetup.mclkOutputEnable = 0
            elif(direction == 'out'):
                if (clk_rate == '48'):
                    clks.audioSetup.sclkFrequency = 0 # 3.072 MHz
                elif (clk_rate == '96'):
                    clks.audioSetup.sclkFrequency = 2 # 6.144 MHz
                elif (clk_rate == '192'):
                    clks.audioSetup.sclkFrequency = 3 # 12.288 MHz
                else:
                    clks.audioSetup.sclkFrequency = 0 # 3.072 MHz
                clks.audioSetup.driveClks = 1
                clks.audioSetup.mclkFrequency = 3 # 12.288 MHz
                clks.audioSetup.mclkOutputEnable = 1
            elif(direction == 'usb'):
                clks.audioSource      = 2 # USB
#                clks.srcAudioRate     = 0   # these struct members don't exist
#                clks.outputAudioRate  = 0   # commented out by mwd 5-15-15
#                clks.sclkOutputSelect = 1
            else:
                print(self.help('i2s_clocks'))
                return
            (status, nul) = self.__device.set_i2s_clocks(clks)
            self.__device.decode_error_status(status, cmd='set_i2s_clocks', print_on_error=True)

    @config('dev_tx', [map(str, range(10))])
    def coef(self, table_id, slave_id='0xff'):
        """Set the active coef table on one or all connected RX devices.

        usage: coef <0-10> [slave_id]

        If slave_id is missing the default value is 0xff which will broadcast
        the command.

        """
        (status, null) = self.__device.coef(int(slave_id,0), int(table_id,0))
        self.__device.decode_error_status(status, cmd='coef(0xff, %s)' % table_id, print_on_error=True)

    @config('restr_tx', [['0'], ['0xff']])
    def delay(self, delay, slave_id='0xff'):
        """Set the bulk delay for a tx device or all RX devices (0xff).

        usage: delay <microsec> [slave_id]

        Default value for slave_id is 0xff which will broadcast the command.

        """
        (status, null) = self.__device.delay(int(slave_id, 0), int(delay, 0))
        self.__device.decode_error_status(status, cmd='delay(%s, %s)' % (slave_id, delay), print_on_error=True)

    @config('dev_tx', [['full', 'fast']])
    def discover(self, type):
        """Issues a full or fast discover.

        usage: discover <full|fast>

        """
        (status, nul) = self.__device.discover(['fast','full'].index(type))
        if(status == 0x01):
            (status, count) = self.__device.slave_count()
            if(status == 0x01):
                print("Discovered %d slaves" % count)
                self.slaves()
            else:
                self.__device.decode_error_status(status, cmd='slave_count()', print_on_error=True)
        else:
            self.__device.decode_error_status(status, cmd='discover(%s)' % type, print_on_error=True)

    @config('dev_tx', [['add']])
    def disco(self, type=None):
        """Combined beacon/discover.

        usage: disco [add]

        add -- performs a full discovery regardless of the number of slaves
               currently known by the master.

        If the current slave count is greater than 0, a beacon+restore is
        performed unless the 'add' option is given in which case a full
        discover is performed.

        """
        (status, count) = self.__device.slave_count()
        print("%d slaves currently discovered" % count)
        if((not type) and (count > 0)):
            if(count > 0): # Fast discovery
                print("Restoring system...")
                self.beacon('5500', '99')
                self.restore()
#                self.discover('fast')
        else: # Full discovery
            if(type == 'add'):
                print("Adding...")
            else:
                print("Full discovery...")
            self.beacon('5500', '99')
            self.discover('full')

    @config('restr_tx', [map(str, range(35)), map(str, range(35))])
    def dfs_set_channel(self, monitor_index, working_index):
        """Set the monitor and working radio channels

        usage: dfs_set_channel <monitor_channel_index> <working_channel_index>

        """
        monitor_index = int(monitor_index,0)
        working_index = int(working_index,0)
        print "Monitor to: %d MHz" % dec.channel_to_freq.get(monitor_index, "unknown")
        print "Working to: %d MHz" % dec.channel_to_freq.get(working_index, "unknown")
        self.__tx_dev.dfs_channel_select(
            monitor_index,
            working_index)

    @config('dev_tx')
    def dfs_state(self):
        """Retrieve the state of the DFS engine.

        usage: dfs_state

        """
        status_message = ['(Not Avail)     ', '(Available)     ',
                          '(DFS Ready)     ', '(CAC Active)    ',
                          '(DFS Active)    ', '(Non-DFS Active)']
        mode_message = ['(Disabled)', '(Non-DFS)', '(DFS)     ']

        (status, state) = self.__device.dfs_get_engine_state()
        self.__device.decode_error_status(status, cmd='dfs_state()', print_on_error=True)
        print 'Channel    Mode            Status \t Non-Occupancy   CAC'
        for i in range(8, desc.RADIOCHAN_MAX_CHANNELS):
            print '  ', state.dfsChannelStatus[i].channel, '\t',
            print state.dfsChannelStatus[i].operatingMode,
            print mode_message[state.dfsChannelStatus[i].operatingMode], '\t',
            print state.dfsChannelStatus[i].channelStatus,
            print status_message[state.dfsChannelStatus[i].channelStatus], '\t',
            print state.dfsChannelStatus[i].nonOccupancyCountdown, '\t ',
            print state.dfsChannelStatus[i].channelAvailabilityCountdown

    @config('restr_tx', [map(str, range(8))])
    def dfs_override(self, override_mask):
        """Enable/Disable DFS Override Features.

        usage: dfs_override < override_mask >

        Set Bit 0 (i.e. write a '1') to disable channel hops
        Set Bit 1 (i.e. write a '2') to enable radar type information
        Set Bit 2 (i.e. write a '4') to disable changes to the transmit power level
        Reset all bits (i.e. write a '0') to restore default settings

        Any combination of bits may be set, valid range for override_mask is 0-7

        """
        if override_mask in map(str, range(8)):
            self.__tx_dev.dfs_override(int(override_mask, 0))
        else:
            print(self.help('dfs_override'))

    @config('restr_tx', [['disable', 'maxchannels', 'maxdistance']])
    def dfs_set_tpm_mode(self, mode):
        """Set the TPM User Mode.

        usage: dfs_set_tpm_mode <disable|maxchannels|maxdistance>

        example:
            > dfs_set_tpm_mode maxchannels

        """
        status = 0x01
        if (mode == 'disable') or (mode == '0'):
            (status, null) = self.__tx_dev.set_tpm_mode(0)
        elif (mode == 'maxchannels') or (mode == '1'):
            (status, null) = self.__tx_dev.set_tpm_mode(1)
        elif (mode == 'maxdistance') or (mode == '2'):
            (status, null) = self.__tx_dev.set_tpm_mode(2)
        else:
            print(self.help('dfs_set_tpm_mode'))

        if(status != 0x01):
            print self.__device.decode_error_status(status)

    @config('restr_tx')
    def dfs_get_tpm_mode(self):
        """Returns the current TPM User Mode.

        usage: dfs_get_tpm_mode

        """
        mode_message = ['Disabled', 'Max Channels', 'Max Distance']
        (status, mode) = self.__tx_dev.get_tpm_mode()
        self.__device.decode_error_status(status, cmd='get_tpm_mode', print_on_error=True)
        if (status == 0x01):
            print "TPM User Mode: " + mode_message[mode]

    @config('dev_tx')
    def dfs_tpm_attributes(self):
        """Retrieve the current TPM region attributes.

        usage: dfs_tpm_attributes

        """

        (status, attributes) = self.__device.get_tpm_attributes()
        self.__device.decode_error_status(status, cmd='dfs_tpm_attributes()', print_on_error=True)
        print "%s" % str(attributes)

    @config('restr_tx')
    def dfs_dump(self, prefix=None):
        """Dump the DFS engine flash data to a file.

        usage: dfs_dump [prefix]

        The auto generated filename will contain the MAC address of the device.
            02-EA-00-00-00-01_dfs.txt

        If an optional prefix is given it will be prepended to the filename:
            > dfs_dump foo
            foo_02-EA-00-00-00-01_dfs.txt

        """
        if(prefix):
            pre = prefix + "_"
        else:
            pre = ""
        filename = pre + self.__device['mac'] + "_dfs.txt"
        filename = re.sub(':','-',filename)

        if(os.path.exists(filename)):
            overwrite = raw_input("%s exists. Overwrite it? [y,n] " % filename)
            if(overwrite.lower() != "y"):
                return

        print separator(self.__device['mac'])
        print "writing DFS data to %s" % filename
        (status, null) = self.__device.dfs_dump(filename)
        if(status == 0x01):
            print "success"
        else:
            print self.__device.decode_error_status(status, 'dfs_dump(%s)' % filename)

    @config('restr_tx', [[_dirs]])
    def dfs_load(self, filename):
        """Load DFS engine parameter flash from file.

        usage: dfs_load <filename>

        """
        if(not os.path.exists(filename)):
            print "%s doesn't exist" % filename
            return

        (status, null) = self.__device.dfs_load(filename)
        if(status == 0x01):
            print "success"
        else:
            print self.__device.decode_error_status(status, 'dfs_load(%s)' % filename)

    @config('dev_tx')
    def down(self):
        """Orderly shutdown of the network.  See also: shutdown"""
        (status, nul) = self.__device.shutdown()
        self.__device.decode_error_status(status, cmd='shutdown()', print_on_error=True)

    @config('dev_tx')
    def echo(self, slave_id, iterations, tries='1'):
        """Send echoes to an RX device, returns success count.

        usage: echo <slave_index> <iterations> [tries]

        iterations:
            The number of times to issue an echo command.

        tries:
            The number of tries the firmware should attempt.
            Defaults to 1

        """
        iterations_int = int(iterations, 0)
        valid_count = 0
        for i in range(iterations_int):
#            print "echo:", self.__device
            (status, null) = self.__device.echo(int(slave_id,0), int(tries,0))
            self.__device.decode_error_status(status, cmd='echo', print_on_error=True)
            if(status == 0x01):
                valid_count += 1
        print "%d/%d - %f%%" % (valid_count, iterations_int, (float(valid_count)/float(iterations))*100)

    @config('dev_tx')
    def forget(self):
        """Restore default configuration."""
        (status, null) = self.__device.save_configuration(1)
        self.__device.decode_error_status(status, cmd='save_configuration(1)', print_on_error=True)


    @config('dev_tx')
    def i2c_xfer_write(self, *args):
        """Write to a remote RX device.

        usage i2c_xfer_write [options] <slave_ID> <dev_addr> <reg_addr> <data...>

        """
        parser = argparse.ArgumentParser(
            prog='i2c_xfer_write',
            description="Generate remote I2C writes on an RX device.")
        parser.add_argument('slave_id', help="summit RX device ID")
        parser.add_argument('dev_addr', help="remote I2C device address")
        parser.add_argument('reg_addr', help="remote I2C device register address")
        parser.add_argument('data', help="data bytes", nargs=argparse.REMAINDER)
        parser.add_argument('--rab', metavar="BYTES", type=int, default=2, help="number of bytes for the device register address. Default: 2")
        parser.add_argument('--clk', metavar="HZ", type=int, default=100000, help="SCLK frequency in Hz Default: 100000")
        try:
            args = parser.parse_args(args)
        except SystemExit as info:
            return

        slave_id = int(args.slave_id, 0)
        dev_addr = int(args.dev_addr, 0)
        reg_addr = int(args.reg_addr, 0)
        data = [int(x, 0) for x in args.data]

        dev_addr = dev_addr & 0xFE # Make it a write

        MAX_I2C_BUFFER_SIZE = 64  # Actual buffer size is 80 bytes!
        length = len(data)
        if (MAX_I2C_BUFFER_SIZE < int(length)):
            print "Truncating data to", MAX_I2C_BUFFER_SIZE
        (status, null) = self.__device.remote_i2c_xfer(
                slave_id=slave_id,
                dev_addr=dev_addr,
                reg_addr=reg_addr,
                bytes=length,
                data=data,
                reg_addr_bytes=args.rab,
                clock_rate=args.clk)
        self.__device.decode_error_status(status, cmd='i2c_xfer_write', print_on_error=True)

        retries = 5
        while (retries):                    # Poll to see if it's done
            (status, xfer_success) = self.__device.remote_i2c_status(slave_id)
            self.__device.decode_error_status(status, cmd='remote_i2c_status', print_on_error=True)
#           cprint("xfer_success: %d" % xfer_success, 'cyan')
            if (xfer_success == 1):
#                print "I2C write finished"
                break
            retries -= 1
#           time.sleep(1)

        if (xfer_success == 0):
            cprint("I2C write failed", 'red')


    @config('dev_tx')
    def i2c_xfer_read(self, *args):
        """Read from a remote RX device.

        usage: i2c_xfer_read [options] <slave_ID> <dev_addr> <reg_addr> <num_bytes>

        """

        parser = argparse.ArgumentParser(
            prog='i2c_xfer_read',
            description="Generate remote I2C reads on an RX device.")
        parser.add_argument('slave_id', help="summit RX device ID")
        parser.add_argument('dev_addr', help="remote I2C device address")
        parser.add_argument('reg_addr', help="remote I2C device register address")
        parser.add_argument('num_bytes', help="bytes to read")
        parser.add_argument('--rab', metavar="BYTES", type=int, default=2, help="number of bytes for the device register address. Default: 2")
        parser.add_argument('--clk', metavar="HZ", type=int, default=100000, help="SCLK frequency in Hz Default: 100000")

        try:
            args = parser.parse_args(args)
        except SystemExit as info:
            return

        slave_id = int(args.slave_id, 0)
        dev_addr = int(args.dev_addr, 0)
        reg_addr = int(args.reg_addr, 0)
        num_bytes = int(args.num_bytes, 0)

        bytes_read = 0;
        if (0 == num_bytes):
            return

        MAX_I2C_BUFFER_SIZE = 64  # FW limits this to 80 bytes max
        dev_addr = dev_addr | 0x01 # Make it a read
        for begin in range(0, num_bytes, MAX_I2C_BUFFER_SIZE):
            size = min(MAX_I2C_BUFFER_SIZE, num_bytes-begin)
            (status, null) = self.__device.remote_i2c_xfer(
                slave_id,
                dev_addr,
                (reg_addr+begin),
                size,
                data=[],
                reg_addr_bytes=args.rab,
                clock_rate=args.clk)
            self.__device.decode_error_status(status, cmd='i2c_xfer_read', print_on_error=True)

            retries = 5
            while (retries):
                xfer_success = 0;
                (status, xfer_success) = self.__device.remote_i2c_status(slave_id)
                self.__device.decode_error_status(status, cmd='remote_i2c_status', print_on_error=True)
#                cprint("xfer_success: %d" % xfer_success, 'cyan')
                if (xfer_success == 1):
                    break
                retries -= 1

            if (xfer_success == 0):
                cprint("I2C read failed.", 'red')
            else:
                (status, ret_list) = self.__device.remote_i2c_read_buf(slave_id)
                self.__device.decode_error_status(status, cmd='remote_i2c_read_buff', print_on_error=True)
                bytes_read = ret_list[0]
                print "bytes_read: %d" % bytes_read
                buffer = ret_list[1]
                print utils.pretty_print_bytes(ret_list[1])


    @config('dev_tx', [[],[],[],[_dirs]])
    def i2c_xfer_file(self, *args):
        """Send file to remote RX device.

        usage: i2c_xfer_file [options] <slave_ID> <dev_addr> <reg_addr> <file_name>

        """
        parser = argparse.ArgumentParser(
            prog='i2c_xfer_file',
            description="Generate remote I2C writes on an RX device using data read from a file.")
        parser.add_argument('slave_id', help="summit RX device ID")
        parser.add_argument('dev_addr', help="remote I2C device address")
        parser.add_argument('reg_addr', help="remote I2C device register address")
        parser.add_argument('filename')
        parser.add_argument('--rab', metavar="BYTES", type=int, default=2, help="number of bytes for the device register address. Default: 2")
        parser.add_argument('--clk', metavar="HZ", type=int, default=100000, help="SCLK frequency in Hz Default: 100000")
        try:
            args = parser.parse_args(args)
        except SystemExit as info:
            return

        slave_id = int(args.slave_id, 0)
        dev_addr = int(args.dev_addr, 0)
        reg_addr = int(args.reg_addr, 0)

        dev_addr = dev_addr & 0xFE # Make it a write

        MAX_I2C_BUFFER_SIZE = 64  # FW limits this to 80 max
        length   = 0
        try:
            with open(args.filename) as file:
                file_list = self._parse_file(file)   # Read file into a list of words
        except IOError as e:
            cprint("I/O error({0}): {1}".format(e.errno, e.strerror), 'red')
            return

        dev_addr = dev_addr & 0xFE
        total_time = 0
        for begin in range(0, len(file_list), MAX_I2C_BUFFER_SIZE):  # Send a chunk at a time
            size = min(MAX_I2C_BUFFER_SIZE, (len(file_list)-begin))
            buffer = file_list[begin:begin+size]
            start_time = time.time()
            (status, null) = self.__device.remote_i2c_xfer(
                    slave_id,
                    dev_addr,
                    (reg_addr+begin),
                    size,
                    buffer,
                    reg_addr_bytes=args.rab,
                    clock_rate=args.clk)
            end_time = time.time()
            xfer_time = end_time - start_time
            self.__device.decode_error_status(status, cmd='remote_i2c_write', print_on_error=True)

#            print("Wrote %d bytes in %f seconds." % (size, xfer_time))

            retries = 5
            while (retries):                    # Poll to see if it's done
                (status, xfer_success) = self.__device.remote_i2c_status(slave_id)
                self.__device.decode_error_status(status, cmd='remote_i2c_status', print_on_error=True)
#               cprint("xfer_success: %d" % xfer_success, 'cyan')
                if (xfer_success == 1):
                    total_time += xfer_time
                    break
                retries -= 1
#               time.sleep(1)

            if (xfer_success == 0):
                cprint("I2C xfer failed", 'red')

        cprint("Total xfer rate: %d bytes/sec" % (len(file_list)/total_time), 'green')

    def _parse_file(self, f):
        """Reads from a file and returns a list of fields"""
        a = list()
        for line in f:
            for byte in line.split():
                a.append(int(byte, 0))
        return a


    @config('restr_tx', [['enable', 'disable']])
    def keep(self, enable):
        """Enable Speaker Keeper.

        usage: keep <enable|disable>

        """
        if((enable == 'enable') or (enable == '1')):
            en = 1
        elif((enable == 'disable') or (enable == '0')):
            en = 0
        (status, null) = self.__device.keep(en)
        self.__device.decode_error_status(status, cmd='keep(%s)' % enable, print_on_error=True)


    @config('dev_tx', [['operating_state', 'descriptor', 'speaker_descriptor', 'wisa', 'key_status']])
    def master(self, mode=None, speaker_index='0'):
        """Print out useful information about the TX device.

        usage: master [operating_state|descriptor|speaker_descriptor|wisa]

        - operating_state
            Print out the operating state descriptor structure.
        - descriptor
            Print out the master descriptor structure.
        - speaker_descriptor
            Print out the master speaker descriptor structure
        - wisa
            Print out the master WiSA descriptor structure
        - key_status
            Print the master key info descriptor structure

        """
        if(mode == 'operating_state'):
            self._master_info_operating_state()
        elif(mode == 'descriptor'):
            self._master_descriptor()
        elif(mode == 'speaker_descriptor'):
            self._master_speaker_descriptor(speaker_index)
        elif(mode == 'wisa'):
            self._master_wisa_descriptor()
        elif(mode == 'key_status'):
            self._master_key_status()
        else:
            self._master_info_basic()

    def _master_wisa_descriptor(self):
        """Print out the master WiSA descriptor"""
        (status, mwd) = self.__tx_dev.get_master_wisa_descriptor()
        if(status == 0x01):
            print("wisaVersion: 0x%.2X" % mwd.wisaVersion)
        else:
            print self.__tx_dev.decode_error_status(status)

    def _master_speaker_descriptor(self, index):
        """Print out the master speaker descriptor"""
        (status, msd) = self.__tx_dev.get_master_speaker_descriptor(int(index,0))
        if(status == 0x01):
            print msd
        else:
            print self.__tx_dev.decode_error_status(status, cmd='get_master_speaker_descriptor(%s)' % index, print_on_error=True)

    def _master_descriptor(self):
        """Print out the master descriptor"""
        (status, md) = self.__tx_dev.get_master_descriptor()
        if(status == 0x01):
            print md
        else:
            print self.__tx_dev.decode_error_status(status)

    def _master_info_operating_state(self):
        """Print out the master operating state information."""
        (status, mos) = self.__tx_dev.get_master_operating_state()
        self.__device.decode_error_status(status, cmd='get_master_operating_state', print_on_error=True)
        if(status == 0x01):
            print mos
        else:
            print self.__tx_dev.decode_error_status(status)

    def _master_key_status(self):
        (status, mks) = self.__tx_dev.get_master_key_status()
        self.__device.decode_error_status(status, cmd='get_master_key_status', print_on_error=True)
        if(status == 0x01):
            print mks
        else:
            print self.__tx_dev.decode_error_status(status)

    def _master_info_basic(self):
        """Print out basic master information."""
        out_str = ""
        (status, dev_id) = self.__tx_dev.rd(0x400008)
        self.__tx_dev.decode_error_status(status, cmd='rd(0x400008)', print_on_error=True)
        if(status == 0x01):
            dev_id = (dev_id & 0x0f)
        (status, gmd) = self.__tx_dev.get_master_descriptor()
        self.__tx_dev.decode_error_status(status, cmd='get_master_descriptor()', print_on_error=True)
        md = gmd.moduleDescriptor
        if(status == 0x01):
            out_str = ":".join(["%.2X" % i for i in md.macAddress]) + " "
            major = md.firmwareVersion >> 5   # (Upper 11-bits)
            minor = md.firmwareVersion & 0x1f # (Lower 5-bits)
            out_str += "- v%d.%d " % (major, minor)
            module_id = (md.moduleID & 0xff)
            out_str += "- %s (0x%.2X) " % (dec.module_id.get(module_id, "Unknown moduleID"), module_id)
            out_str += "- %s (0x%.2X) " % (dec.hardware_type.get(md.hardwareType, "Unknown hardwareType"), md.hardwareType)
        print(out_str)

    @config('dev_tx', [['all_slaves', 'master']])
    def reset(self, *devs):
        """Reset TX or RX device(s).

        usage: reset [<all_slaves|slave_index [slave_index ...]>]

        Without any arguments the TX device will be reset via a GPIO pin driven
        from the Raspberry Pi. Reseting the TX device will only function if the
        external reset pin of the TX device is connected to the Raspberry Pi
        GPIO defined as an external reset signal.

        """
        if(devs):
            if(devs[0] == 'all_slaves'):
                (status, nul) = self.__device.reset(0xFF)
                self.__device.decode_error_status(status, cmd='reset(0xFF)', print_on_error=True)
            else:
                ids = self._order_list(map(int, devs))
                for slave_id in ids:
                    print "resetting: %d" % slave_id
                    (status, nul) = self.__device.reset(slave_id)
                    self.__device.decode_error_status(status, cmd='reset(%d)' % slave_id, print_on_error=True)
        else:
            print "resetting master..."
            (status, nul) = self.__device.gpio_reset()
            self.__device.decode_error_status(status, cmd='gpio_reset()', print_on_error=True)

    @config('dev_tx')
    def restore(self):
        """Restore saved speakers."""
        (status, null) = self.__device.restore()
        self.__device.decode_error_status(status, cmd='restore()', print_on_error=True)

    @config('dev_tx')
    def save(self, type='0'):
        """Save current configuration.

        usage: save [0|1]

        0:
            Save a system configuration to flash. This is the default option.
        1:
            Forget a saved system. The "forget" command is an alias for "save 0".

        """
        (status, null) = self.__device.save_configuration(int(type,0))
        self.__device.decode_error_status(status, cmd='save_configuration(%s)' % type, print_on_error=True)

    @config('dev_tx')
    def shutdown(self):
        """Orderly shutdown of the network.  See also: down"""
        (status, null) = self.__device.shutdown()
        self.__device.decode_error_status(status, cmd='shutdown()', print_on_error=True)

    @config('dev_tx', [[],['operating_state', 'module_descriptor', 'speaker_descriptor',
        'wisa', 'global_coefficient_data', 'current_coefficient_data',
        'speaker_key_status']])
    def slave_info(self, slave_index, mode, spkr_desc_index='0'):
        """Print descriptors for a particular RX device

        usage: slave_info <slave_index> <descriptor_type> [spkr_index]

        Descriptor types:
            - operating_state
            - module_descriptor
            - speaker_descriptor
            - wisa
            - global_coefficient_data
            - current_coefficient_data
            - speaker_key_status

        """
        slave_index = int(slave_index,0)
        spkr_desc_index = int(spkr_desc_index,0)
        if(mode == 'operating_state'):
            (status, descriptor) = self.__tx_dev.get_speaker_operating_state(slave_index,
                self.__test_profile.getboolean('SETTINGS', 'get_from_network'))
        elif(mode == 'module_descriptor'):
            (status, descriptor) = self.__tx_dev.get_speaker_module_descriptor(slave_index,
                self.__test_profile.getboolean('SETTINGS', 'get_from_network'))
        elif(mode == 'speaker_descriptor'):
            (status, descriptor) = self.__tx_dev.get_speaker_descriptor(slave_index,
                self.__test_profile.getboolean('SETTINGS', 'get_from_network'), spkr_desc_index)
        elif(mode == 'wisa'):
            (status, descriptor) = self.__tx_dev.get_speaker_wisa_descriptor(slave_index,
                self.__test_profile.getboolean('SETTINGS', 'get_from_network'))
        elif(mode == 'global_coefficient_data'):
            (status, descriptor) = self.__tx_dev.get_speaker_global_coefficient_data_descriptor(slave_index,
                self.__test_profile.getboolean('SETTINGS', 'get_from_network'))
        elif(mode == 'current_coefficient_data'):
            (status, descriptor) = self.__tx_dev.get_speaker_current_coefficient_data_descriptor(slave_index,
                self.__test_profile.getboolean('SETTINGS', 'get_from_network'))
        elif(mode == 'speaker_key_status'):
            (status, descriptor) = self.__tx_dev.get_speaker_key_status(slave_index,
                self.__test_profile.getboolean('SETTINGS', 'get_from_network'))
        else:
            print(self.help('slave_info'))
            return

        if(status == 0x01):
            print descriptor
        else:
            self.__logger.error(self.__tx_dev.decode_error_status(status))


    @config('dev_tx', [['operating_state', 'location']])
    def slaves(self, mode=None):
        """Print out concise details for slaves known by the master.

        usage: slaves [operating_state|location]

        """
        (status, current_zone) = self.__tx_dev.get_speaker_zone()

        for zone in range(8):
            (status, null) = self.__tx_dev.set_speaker_zone(zone)
            if(status == 0x01):
                (status, slave_count) = self.__tx_dev.slave_count()
                self.__device.decode_error_status(status, cmd='slave_count', print_on_error=True)
                if(status == 0x01):
                    if (slave_count > 0):
                        if(zone > 0):
                            print ""
                        cprint("Zone %d: %d %s" %
                            (zone, slave_count, "slave" if slave_count == 1 else "slaves"), 'cyan')
                else:
                    return

                for slave_index in range(slave_count):
                    if(mode == 'operating_state'):
                        self._slave_info_operating_state(slave_index)
                    elif(mode == 'location'):
                        self._slave_info_location(slave_index)
                    else:
                        self._slave_info_basic(slave_index)
        (status, null) = self.__tx_dev.set_speaker_zone(current_zone)
        self.__device.decode_error_status(status, cmd='set_speaker_zone(%d)' % current_zone, print_on_error=True)


    def _slave_info_location(self, slave_index):
        """Print out the slave location information."""
        if(slave_index == 0):
            print '  {:3}   {:^5} {:^5}   {:^5}  {:<13}  {:<5}'.format(
                "", "X", "Y", "Vector", "Type", "Delays")

        (status, sif) = self.__tx_dev.get_master_speaker_location_descriptor(
            slave_index)
        self.__device.decode_error_status(status, cmd='get_master_speaker_location_descriptor(%s)' % slave_index, print_on_error=True)
        if(status == 0x01):
            out_str = '  {:<2}:  ({:5},{:5})  @{:<5}  {:13}  raw: {:5}  cal: {:5}'.format(
                slave_index,
                sif.speakerX,
                sif.speakerY,
                sif.speakerVectorDistance,
                dec.speaker_types.get(sif.speakerType, "Unknown"),
                sif.rawSpeakerDelay,
                sif.calSpeakerDelay,
                )
            print out_str

    def _slave_info_operating_state(self, slave_index):
        """Print out the slave operating state information."""
        (status, smd) = self.__tx_dev.get_speaker_module_descriptor(
            slave_index,
            self.__test_profile.getboolean('SETTINGS', 'get_from_network')
        )
        self.__device.decode_error_status(status, cmd='get_speaker_module_descriptor(%s)' % slave_index, print_on_error=True)

        (status, slave_count) = self.__tx_dev.slave_count()
        self.__device.decode_error_status(status, cmd='slave_count()', print_on_error=True)

        (status, sos) = self.__tx_dev.get_speaker_operating_state(
            slave_index,
            self.__test_profile.getboolean('SETTINGS', 'get_from_network')
            )
        self.__device.decode_error_status(status, cmd='get_master_operating_state(%s, 0)' % slave_index, print_on_error=True)
        if(status == 0x01):
            out_str = "  %d: " % slave_index
            out_str += ":".join(["%.2X" % i for i in smd.macAddress]) + " "
            out_str += "- %s " % dec.system_mode.get(sos.slaveMode, 'unknown')
            out_str += "- Slot %d " % sos.slaveAssignment
            out_str += "- %s " % (['Linear', 'Normalized'][sos.volumeInfo.tableID])
            out_str += "- 0x%.5X " % (sos.volumeInfo.volume)
            out_str += "- Amp %s " % (['Fault', 'OK'][sos.amplifierOK])
            print(out_str)

    def _slave_info_basic(self, slave_index):
        """Print out basic slave information."""
        out_str = ""
        (status, smd) = self.__tx_dev.get_speaker_module_descriptor(
            slave_index,
            self.__test_profile.getboolean('SETTINGS', 'get_from_network'))
        self.__device.decode_error_status(status, cmd='get_speaker_module_descriptor(%s)' % slave_index, print_on_error=True)
        if(status == 0x01):
            out_str = "  %d: " % slave_index
            out_str += ":".join(["%.2X" % i for i in smd.macAddress]) + " "
            major = smd.firmwareVersion >> 5   # (Upper 11-bits)
            minor = smd.firmwareVersion & 0x1f # (Lower 5-bits)
            out_str += "- v%d.%d " % (major, minor)
            module_id = (smd.moduleID & 0xff)
            out_str += "- %s (0x%.2X) " % (dec.module_id.get(module_id, "Unknown moduleID"), module_id)
            out_str += "- %s (0x%.2X) " % (dec.hardware_type.get(smd.hardwareType, "Unknown hardwareType"), smd.hardwareType)
        print(out_str)

    @config('dev_tx')
    def slot(self, slave_index, slot):
        """Set the audio slot on a particular RX device.

        usage: slot <slave_index> <audio_slot>

        """
        slave_index_int = int(slave_index, 0)
        slot_int = int(slot, 0)
        (status, null) = self.__device.slot(slave_index_int, slot_int)
        self.__device.decode_error_status(status, cmd='slot(%d, %d)' % (slave_index_int, slot_int), print_on_error=True)

    @config('dev_tx')
    def start(self):
        """Start isoc mode."""
        (status, null) = self.__device.start()
        self.__device.decode_error_status(status, cmd='start()', print_on_error=True)

    @config('dev_tx')
    def stop(self):
        """Stop isoc mode."""
        (status, null) = self.__device.stop()
        self.__device.decode_error_status(status, cmd='stop()', print_on_error=True)

    @config('dev_tx')
    def volume(self, table=None, vol=None):
        """Set the volume on all slaves.

        usage: volume <0|1> <0x20_bit_volume>

        example:
            Max volume without normalization:
                volume 0 0xFFFFF

            Max volume with normalization via custom volume table:
                volume 1 0x7FF

        """
        if (vol != None):
            (status, null) = self.__device.volume(int(table,0), int(vol,0))
        elif (table == None):
            (status, volume) = self.__tx_dev.get_volume()
            if (status == 0x01):
                print "Table: %d  Volume: 0x%.5X" % (volume.tableID, volume.volume)
        else: # got table but no vol
            print(self.help('volume'))
            return
        self.__device.decode_error_status(status, cmd='volume(%s, %s)' % (table, vol), print_on_error=True)

    @config('dev_tx', [['on', 'off']])
    def mute(self, option=None):
        """Turn Mute on/off

        usage: mute [on|1|off|0]

        """
        if (option):
            if((option.lower() == 'on') or (option == '1')):
                en = 1
            elif((option.lower() == 'off') or (option == '0')):
                en = 0
            else:
                print(self.help('mute'))
                return
            (status, null) = self.__device.mute(en)
        else:
            (status, mute) = self.__tx_dev.get_mute()
            if (status == 0x01):
                if (mute == 0x01):
                    print "enabled"
                elif (mute == 0x00):
                    print "disabled"
                else:
                    print "Mute: 0x%.2" % mute
        self.__device.decode_error_status(status, cmd='mute(%s)' % option, print_on_error=True)

#    @config('dev_tx', [['on', 'off']])
#    def power(self, option, volume='0x1000'):
#        """power On the System"""
#
#        (status, count) = self.__device.slave_count()
#        if(count == 0):
#          return
#
#        # Power UP
#        if((option == 'on') or (option == '1')):
#            self.disco()
#            (status, null) =  self.__device.restore()
#            self._decode_error_status(status, cmd='restore()')
#
#            (status, null) = self.__device.start()
#            self._decode_error_status(status, cmd='start()')
#
#            (status, null) = self.__device.volume(int('0',0), int(volume,0))
#            self._decode_error_status(status, cmd='volume(0, %s)' % volume)
#
#        # Power DOWN
#        elif((option == 'off') or (option == '0')):
#
#            (status, null) =  self.__device.shutdown()
#            self._decode_error_status(status, cmd='shutdown()')
#
#            (status, null) =  self.__device.reboot()
#            self._decode_error_status(status, cmd='reboot()')

    @config('dev_tx')
    def set_rx_mac(self, index, mac):
        """Set RX MAC Address for specified Slave.

        This command is used to overwrite MAC address entries in the slave
        table of the TX device.

       usage: set_rx_mac <device_index> <MAC>

       example:
           set_rx_mac 0 00:11:22:33:44:55

        """
        macaddress = map(myint, mac.split(':'))
        (status, null) =  self.__device.setRxMAC(index, macaddress)
        self.__device.decode_error_status(status, cmd='setRxMAC()', print_on_error=True)

    @config('dev_tx')
    def reboot(self):
        """Externally reset the TX device.

        This command will only function if the external reset pin of the TX
        device is connected to the Raspberry Pi GPIO defined as an external
        reset signal.

        """

        (status, null) = self.__device.reboot()
        self.__device.decode_error_status(status, cmd='reboot', print_on_error=True)

        (status, zone_number) = self.__tx_dev.get_speaker_zone()
        self.__device.decode_error_status(status, cmd='get_speaker_zone', print_on_error=True)

    @config('dev_tx', [[_dirs]])
    def push_map(self, filename, enable_old_style="0"):
        """Push a speaker map.

        usage: push_map <config_file.cfg> [0|1]

        example:
            Push a map from a system profile file 'sys.cfg':
                push_map sys.cfg

            Push the same map using the old style API (single push):
                push_map sys.cfg 1

        """

        enable_old_style = bool(int(enable_old_style))

        tp = testprofile.TestProfile()
        if(os.path.exists(filename)):
            tp.readfp(open(filename))
        else:
            self.__logger.error("No such file: %s" % filename)
        print "validating %s" % (filename)
        if(tp.validate()):
            if enable_old_style:
                self.__logger.warning("pushing with old style API")
                num_speakers = 1
            else:
                num_speakers = len(tp)
            self.__tx_dev.push_map_profile(tp, num_speakers)
        print "done."

    @config('dev_tx')
    def get_map_type(self):
        """Query the current map type"""
        (status, map_type) = self.__tx_dev.get_map_type()
        if(status == 0x01):
            print "%s (%d)" % (dec.map_types.get(map_type, "unknown"), map_type)

    @config('dev_tx', [map(str, range(10))])
    def zone(self, zone_number=None):
        if(zone_number):
            zone_number = int(zone_number, 0)
            (status, null) = self.__tx_dev.set_speaker_zone(zone_number)
            self.__device.decode_error_status(status, cmd='set_speaker_zone(%d)' % zone_number, print_on_error=True)
        else:
            (status, zone_number) = self.__tx_dev.get_speaker_zone()
            self.__device.decode_error_status(status, cmd='get_speaker_zone', print_on_error=True)
            if(status == 0x01):
                print zone_number

    @config('dev_tx')
    def move_to_zone(self, slave_id, new_zone):
        """Move a slave to a new zone.

        usage: move_to_zone <rx_id> <new_zone>

        """
        slave_id = int(slave_id, 0)
        new_zone = int(new_zone, 0)

        (status, null) = self.__tx_dev.move_speaker_zone(slave_id, new_zone)
        self.__device.decode_error_status(status, cmd="move_speaker_zone(%d, %d)" % (slave_id, new_zone), print_on_error=True)


    @config('dev_tx')
    def chime(self, slave_id, tone='0x09', duration='3000'):
        """Start a tone or "white" noise on an RX device

        usage: chime <slave_id> [tone_value] [duration in ms]

        defaults:
            tone_value -- 9       (960Hz)
            duration   -- 3000 ms (3 seconds)

        Valid tone values:
            0:  24 Hz
            1:  48 Hz
            2:  96 Hz
            3:  120 Hz
            4:  192 Hz
            5:  240 Hz
            6:  384 Hz
            7:  480 Hz
            8:  600 Hz
            9:  960 Hz
            10: 1200 Hz
            11: 1920 Hz
            12: 2400 Hz
            13: 3000 Hz
            14: 4800 Hz
            15: 6000 Hz
            16: 9600 Hz
            17: 12000 Hz
            18: 24000 Hz
            19: White Noise
        """
        slave_id = int(slave_id, 0)
        duration = int(duration, 0)
        tone = int(tone, 0)

        index_to_frequency = {
            0:  "24 Hz",
            1:  "48 Hz",
            2:  "96 Hz",
            3:  "120 Hz",
            4:  "192 Hz",
            5:  "240 Hz",
            6:  "384 Hz",
            7:  "480 Hz",
            8:  "600 Hz",
            9:  "960 Hz",
            10: "1200 Hz",
            11: "1920 Hz",
            12: "2400 Hz",
            13: "3000 Hz",
            14: "4800 Hz",
            15: "6000 Hz",
            16: "9600 Hz",
            17: "12000 Hz",
            18: "24000 Hz",
            19: "White Noise"}

        if(tone not in index_to_frequency):
            print "invalid tone"
            return

        if(duration > 30000):
            print "30 seconds is the max duration"
            return

        print "%s for %sms" % (index_to_frequency.get(tone, "invaild tone"), duration)

        (status, null) = self.__device.chime(slave_id, tone, duration)
        self.__device.decode_error_status(status, cmd="chime(%d, %d, %d" % (slave_id, duration, tone), print_on_error=True)


    @config('dev_rx')
    def chime_rx(self, tone='9', duration='3000', volume='0x1000'):
        """Start a tone or "white" noise on an RX device

        usage: chime [tone_value] [duration] [volume]

        defaults:
            tone_value -- 9       (960Hz)
            duration   -- 3000    (3 seconds)
            volume     -- 0x1000

        Valid tone values:
            0:  24 Hz
            1:  48 Hz
            2:  96 Hz
            3:  120 Hz
            4:  192 Hz
            5:  240 Hz
            6:  384 Hz
            7:  480 Hz
            8:  600 Hz
            9:  960 Hz
            10: 1200 Hz
            11: 1920 Hz
            12: 2400 Hz
            13: 3000 Hz
            14: 4800 Hz
            15: 6000 Hz
            16: 9600 Hz
            17: 12000 Hz
            18: 24000 Hz
            19: White Noise
        """
        TURN_OFF = 0xFF
        tone     = int(tone, 0)
        duration = int(duration, 0)
        volume   = int(volume, 0)

        index_to_frequency = {
            0:  "24 Hz",
            1:  "48 Hz",
            2:  "96 Hz",
            3:  "120 Hz",
            4:  "192 Hz",
            5:  "240 Hz",
            6:  "384 Hz",
            7:  "480 Hz",
            8:  "600 Hz",
            9:  "960 Hz",
            10: "1200 Hz",
            11: "1920 Hz",
            12: "2400 Hz",
            13: "3000 Hz",
            14: "4800 Hz",
            15: "6000 Hz",
            16: "9600 Hz",
            17: "12000 Hz",
            18: "24000 Hz",
            19: "White Noise"}

        if(tone not in index_to_frequency):
            print "invalid tone"
            return

        if(duration > 30000):
            print "30 seconds is the max duration"
            return

        print "%s for %sms at vol 0x%.5X" % (index_to_frequency.get(tone, "invaild tone"), duration, volume)

        (status, null) = self.__device.chime_rx(tone, volume)
        self.__device.decode_error_status(status, cmd="chime_rx(%d, 0x%.5X" % (tone, volume), print_on_error=True)
        time.sleep(duration/1000)
        (status, null) = self.__device.chime_rx(TURN_OFF, volume)
        self.__device.decode_error_status(status, cmd="chime_rx(%d, 0x%.5X" % (TURN_OFF, volume), print_on_error=True)


    @config('dev_tx', [['enable', 'disable']])
    def autostart(self, enable=None):
        """Enable/Disable auto startup on next reboot.

        usage: autostart [enable|1|disable|0]

        """
        if not enable:
            (status, mos) = self.__tx_dev.get_master_operating_state()
            if(status != 0x01):
                self.__device.decode_error_status(status, cmd='get_master_operating_state', print_on_error=True)

            auto_start = (mos.speakerKeeperState >> 1) & 0x1
            print "%s" % {0: "disabled", 1: "enabled"}.get(auto_start, "unknown")
        else:
            if (enable == "enable") or (enable == '1'):
                enable_int = 1
            elif (enable == "disable") or (enable == '0'):
                enable_int = 0

            (status, null) = self.__tx_dev.autostart(enable_int)
            self.__device.decode_error_status(status, cmd='autostart(%d)' % enable_int, print_on_error=True)

    @config('dev_tx')
    def set_vol_trim(self, device_id, vol_trim):
        """Set the volume trim for device.

        usage: set_vol_trim <device_id> [-]<16-bit trim value>

        example:
                set_vol_trim 0 -6

        """
        (status, null) = self.__device.set_volume_trim(int(device_id,0), int(vol_trim,0))
        self.__device.decode_error_status(status, cmd='set_vol_trim(%s, %s)' % (device_id, vol_trim), print_on_error=True)

    @config('dev_tx')
    def get_vol_trim(self, device_id):
        """Request master to retrieve volume trim from device

        usage: get_vol_trim <device_id>

        """
        (status, log_vol_trim) = self.__device.get_volume_trim(int(device_id,0))
        self.__device.decode_error_status(status, cmd='get_vol_trim', print_on_error=True)
        if (status == 0x01):
            if (log_vol_trim < 0):
                print "Volume trim: -0x%.5X" % abs(log_vol_trim)
            else:
                print "Volume trim: 0x%.5X" % log_vol_trim


    @config('dev_tx')
    def set_ir_filter(self, address):
        """Set a software IR filter address

        usage: set_ir_filter <2-byte address>

        """
        address = int(address, 0)
        (status, null) =  self.__device.set_ir_filter(address)
        self.__device.decode_error_status(status, cmd='set_ir_filter(%d)' % address, print_on_error=True)


    @config('dev_tx')
    def set_block_events_enable(self, enable=1):
        """Request master to block key slave events (1)

        usage: set_block_events_enable [0 | 1]
        """
        enable = (int(enable, 0) & 0x1)
        (status, null) = self.__device.set_block_events_enable(enable)
        self.__device.decode_error_status(status, cmd='set_block_events_enable(%d)' % enable, print_on_error=True)


    @config('dev_tx')
    def get_block_events_enable(self):
        """Retrieve the block events enable byte from the master

        usage: get_block_events_enable
        """
        (status, enable) = self.__device.get_block_events_enable()
        self.__device.decode_error_status(status, cmd='get_block_events_enable()', print_on_error=True)
        if (0x01 == status):
            print "Block events enable: %d" % enable

    @config('dev_tx', [['enable', 'disable']])
    def rx_control(self, enable=None):
        """Enable/Disable @RX control

        usage: rx_control [enable|1|disable|0]

        """
        if not enable:
            (status, mos) = self.__tx_dev.get_master_operating_state()
            if(status != 0x01):
                self.__device.decode_error_status(status, cmd='get_master_operating_state', print_on_error=True)

            rxcontrol = (mos.speakerKeeperState >> 2) & 0x1
            print "%s" % {0: "disabled", 1: "enabled"}.get(rxcontrol, "unknown")
        else:
            if (enable == "enable") or (enable == '1'):
                enable_int = 1
            elif (enable == "disable") or (enable == '0'):
                enable_int = 0
            else:
                print(self.help('rx_control'))
                return

            (status, null) = self.__tx_dev.set_rx_control(enable_int)
            self.__device.decode_error_status(status, cmd='rx_control(%d)' % enable_int, print_on_error=True)

    @config('dev_tx', [map(str, range(10))])
    def max_zone(self, zone_number=None):
        """Set/Get Max zone application supports

        usage: max_zone [0-7]

        """
        if(zone_number):
            zone_number = int(zone_number, 0)
            (status, null) = self.__tx_dev.set_max_zone(zone_number)
            self.__device.decode_error_status(status, cmd='set_max_zone(%d)' % zone_number, print_on_error=True)
        else:
            (status, mos) = self.__tx_dev.get_master_operating_state()
            if(status != 0x01):
                self.__device.decode_error_status(status, cmd='get_master_operating_state', print_on_error=True)

            zone_number = (mos.speakerKeeperState >> 3) & 0x7
            print zone_number

    @config('dev_tx', [['true', 'false']])
    def led_disable(self, disable=None):
        """Disable/Enable lighting of the Isoch and Heartbeat LEDs.

        usage: led_disable [true|1|false|0]

        """
        if not disable:
            (status, disable) = self.__tx_dev.get_led_disable()
            if(status != 0x01):
                self.__device.decode_error_status(status, cmd='get_led_disable', print_on_error=True)

            print "%s" % {0: "LEDs On", 1: "LEDs Off"}.get(disable, "unknown")
        else:
            if (disable == "true") or (disable == '1'):
                disable_int = 1
            elif (disable == "false") or (disable == '0'):
                disable_int = 0

            (status, null) = self.__tx_dev.set_led_disable(disable_int)
            self.__device.decode_error_status(status, cmd='led_disable(%d)' % disable_int, print_on_error=True)


#==============================================================================
# Multi-Master Methods
#==============================================================================
    @config('dev_tx')
    def get_master_macs(self, slave_id):
        """Retrieve the available TX device MAC addresses known by the RX device

        usage: get_master_macs <slave_id>

        """
        slave_id = int(slave_id, 0)
        (status, macs) = self.__tx_dev.get_master_macs(slave_id)
        self.__device.decode_error_status(status, "get_master_macs(%d)" % slave_id, print_on_error=True)
        for mac in range(len(macs)):
            print "%d: %s" % (mac, macs[mac])

    @config('dev_tx')
    def set_rx_mac(self, index, mac):
        """Set RX MAC Address for specified Slave.

        This command is used to overwrite MAC address entries in the slave
        table of the TX device.

       usage: set_rx_mac <device_index> <MAC>

       Example:
           set_rx_mac 0 00:11:22:33:44:55

        """
        macaddress = map(myint, mac.split(':'))
        (status, null) =  self.__device.setRxMAC(index, macaddress)
        self.__device.decode_error_status(status, cmd='setRxMAC()', print_on_error=True)

    @config('dev_tx')
    def add_master_mac(self, device_index, mac):
        """Add Master MAC Address for specified Slave.

        This command is used to add a mster  MAC address

       usage: add_master_mac <device_index> <MAC>

       Example:
           add_master_mac 0 00:11:22:33:44:55

        """
        device_index = int(device_index, 0)
        macaddress = map(myint, mac.split(':'))
        (status, null) =  self.__device.add_master_mac(device_index, macaddress)
        self.__device.decode_error_status(status, cmd='addMasterMAC()', print_on_error=True)

    @config('dev_tx')
    def remove_master_mac(self, index, mac):
        """Remove Master MAC Address for specified Slave.

        This command is used to remove a mster  MAC address

       usage: remove_master_mac <device_index> <MAC>

       Example:
           remove_master_mac 0 00:11:22:33:44:55

        """
        index = int(index, 0)
        macaddress = map(myint, mac.split(':'))
        (status, null) =  self.__device.remove_master_mac(index, macaddress)
        self.__device.decode_error_status(status, cmd='removeMasterMAC()', print_on_error=True)

    @config('dev_tx')
    def assign_master_mac(self, device_index, master_number):
        """Tell an RX device to respond to a particular TX device MAC

        usage: assign_master_mac <device_index> <master_number>

        """
        device_index = int(device_index, 0)
        master_number = int(master_number, 0)
        (status, null) = self.__tx_dev.assign_master_mac(device_index, master_number)
        self.__device.decode_error_status(status, cmd='assign_master_mac(%d, %d)' % (device_index, master_number), print_on_error=True)

    @config('restr_tx')
    def dump_system_data(self, prefix=None):
        """Dump the system data to a file.

        usage: [[MAC].]dump_system_data [prefix]

        The auto generated filename will contain the MAC address of the device.
            02-EA-00-00-00-01_sys.txt

        If an optional prefix is given it will be prepended to the filename:
            > dump_system_data foo
            foo_02-EA-00-00-00-01_sys.txt

        """
        if(prefix):
            pre = prefix + "_"
        else:
            pre = ""
        filename = pre + self.__device['mac'] + "_sys.txt"
        filename = re.sub(':','-',filename)

        if(os.path.exists(filename)):
            overwrite = raw_input("%s exists. Overwrite it? [y,n] " % filename)
            if(overwrite.lower() != "y"):
                return

        print separator(self.__device['mac'])
        print "writing system data to %s..." % filename
        (status, buffer) = self.__device.get_coefficient_data()
        if(status == 0x01):
            buffer.write(filename)
            print "success"
        else:
            print self.__device.decode_error_status(status)

#==============================================================================
# RX Device Commands
#==============================================================================
    @config('dev_rx')
    def get_coeffs(self):
        """Print out the biquad coefficient values currently in RAM.

        usage: [[MAC].]get_coeffs

        """
        bands = ['High:', 'Mid:', 'Low:']
        quad_coef_addr = 0x40707c
        (status, low) = self.__device.wr(quad_coef_addr, 0x00)
        self.__device.decode_error_status(status, cmd='rd', print_on_error=True)
        print "%s" % self.__device['mac']
        for band in range(3):
            print "  " + bands[band]
            for biquad in range(12):
                coef_str = "   "
                for coef in range(5):
                    (status, low)  = self.__device.rd(quad_coef_addr+4)
                    self.__device.decode_error_status(status, cmd='rd', print_on_error=True)
                    (status, high) = self.__device.rd(quad_coef_addr+8)
                    self.__device.decode_error_status(status, cmd='rd', print_on_error=True)
                    coef_str += "0x%.6X " % ((high << 16) + low)
                print coef_str
            print ""

#==============================================================================
# Common Commands
#==============================================================================
    @config('dev_all')
    def get_radio_channel(self):
        """Returns the current radio channel.

        usage: [[MAC].]get_radio_channel

        """
        (status, channel) = self.__device.get_radio_channel()
        self.__device.decode_error_status(status, cmd='get_radio_channel', print_on_error=True)
        return "Ch.%d - %dMHz" % (channel, dec.channel_to_freq.get(channel, "Unknown channel"))

    @config('dev_all')
    def get_src_mac(self):
        """Returns the SRC_MAC for the device.

        usage: [[MAC].]get_src_mac

        """
        (status, mac) = self.__device.get_src_mac()
        self.__device.decode_error_status(status, cmd='get_src_mac', print_on_error=True)
        return "%s" % mac

    @config('dev_all')
    def put_src_mac(self, src_mac):
        """Writes the SRC_MAC to the given device.

        usage: [[MAC].]put_src_mac <MAC>

        example:
            put_src_mac 01:02:03:04:05:06

        """
        if(len(src_mac.split(':')) != 6):
            print "Invalid src_mac format"
            print "Example valid SRC MAC:"
            print "  02:EA:00:00:00:01"
            return
        (status, null) = self.__device.put_src_mac(src_mac)

    @config('dev_all')
    def get_our_mac(self):
        """Returns OUR_MAC for the device.

        usage: [[MAC].]get_our_mac

        """
        (status, mac) = self.__device.get_our_mac()
        self.__device.decode_error_status(status, cmd='get_our_mac', print_on_error=True)
        return "%s" % mac

    @config('restr_all')
    def rd(self, addr):
        """Read a register.

        usage: [MAC][.]rd <0xaddr>

        example:
            TX device usage:
                rd <0xaddr>

            RX device(s) usage:
                [MAC].rd <0xaddr>

        """
        (status, data) = self.__device.rd(int(addr,0))
        if(status == 0x01):
            return "0x%.4X" % data
        else:
            self.__device.decode_error_status(status, cmd='rd(%s)' % addr, print_on_error=True)

    @config('restr_all')
    def switch_image(self):
        """Switch to the alternate firmware image

        usage: [[MAC].]switch_image <slave_index> <image_number>

        """
        slave = 0xfe
        (status, active_image) = self.__device.get_active_image(slave)
        if(status != 1):
            return (status, None)

        if(active_image == 0):
            image = 1
        elif(active_image == 1):
            image = 0
        else:
            return (-1, None)

        (status, image_ok) = self.__device.check_active_image(slave, image)
        if((status == 0x01) and (image_ok == 1)):
            (status, null) = self.__device.set_active_image(slave, image)
        else:
            print "image didn't check out: %s" % (self.__device.decode_error_status())


    @config('dev_all')
    def set_image(self, slave, image):
        """Set the active firmware image.

        usage: [[MAC].]set_image <slave_index> <image_number>

        """
        slave = int(slave, 0)
        image = int(image, 0)
        (status, image_ok) = self.__device.check_active_image(slave, image)
        if ((status == 0x01) and (image_ok == 1)):
            (status, null) = self.__device.set_active_image(slave, image)
            self.__device.decode_error_status(status, cmd='set_image(%d, %d)' % (slave, image), print_on_error=True)
        else:
            print "Image fails validation: 0x%.2X" % status

    @config('dev_all')
    def get_image(self):
        """Print the number of the active image.

        usage: [[MAC].]get_image

        """
        slave = 0xFE
        (status, active_image) = self.__device.get_active_image(slave)
        self.__device.decode_error_status(status, cmd='get_active_image(%s)' % slave, print_on_error=True)
        print "Active image: %d" % active_image

    @config('dev_all')
    def verify_fw_image(self, image_number):
        """Run a check of either firmware image contained in flash.

        usage: [[MAC].]verify_fw_image <image_number>

        image_number: 0 or 1

        """
        image_number = int(image_number, 0)
        (status, image_ok) = self.__device.check_active_image(0xFE, image_number)
        self.__device.decode_error_status(status, 'check_active_image', print_on_error=True)
        if image_ok == 0x01:
            print "Image OK"
        else:
            print "Failed: 0x%.2X" % image_ok

    @config('restr_all')
    def wr(self, addr, data):
        """Write a register.

        usage: [[MAC].]wr <0xaddr> <0xdata>

        example:
            TX device usage:
                wr <0xaddr> <0xdata>

            RX device(s) usage:
                02:EA:00:00:00:01.wr <0xaddr> <0xdata>

        """
        (status, nul) = self.__device.wr(int(addr,0), int(data, 0))
        self.__device.decode_error_status(status, cmd='wr(%s, %s)' % (addr, data), print_on_error=True)

    @config('restr_all')
    def wrr(self, addr, data, radio='working'):
        """Write a radio (Airoha) register.

        usage: [[MAC].]wrr <0xaddr> <0xdata> [working|monitor]

        example:
            TX device usage (default to working radio):
                wrr <0xaddr> <0xdata>
            TX device usage (specify monitor radio):
                wrr <0xaddr> <0xdata> 1

            RX device(s) usage:
                02:EA:00:00:00:01.wrr <0xaddr> <0xdata>

        """
        if (radio == 'working') or (radio == '0'):
            (status, nul) = self.__device.wrr(0, int(addr, 0), int(data, 0))
        #    self.__device.decode_error_status(status, cmd='wrr(%s, %s, %s)' % (addr, data, radio), print_on_error=True)
        elif (radio == 'monitor') or (radio == '1'):
            (status, nul) = self.__device.wrr(1, int(addr, 0), int(data, 0))
        #    self.__device.decode_error_status(status, cmd='wrr(%s, %s, %s)' % (addr, data, radio), print_on_error=True)
        else:
            print(self.help('wrr'))

    @config('restr_all')
    def get_duty_cycle(self):
        """Returns the TX packet duty cycle, express as a percentage.

        usage: [[MAC].]get_duty_cycle

        """
        (status, duty_cycle) = self.__device.get_duty_cycle()
        self.__device.decode_error_status(status, cmd='get_duty_cycle', print_on_error=True)
        return "%d %%" % (duty_cycle)

    def _fw_prep(self, filename):
        """Prep master for firmware push"""
        if(os.path.exists(filename)):
            print("Loading %s ..." % filename)
        else:
            print("No such file: %s" % filename)
            return False

        (status, mos) = self.__tx_dev.get_master_operating_state()
        if(status != 0x01):
            self.__device.decode_error_status(status, cmd='get_master_operating_state', print_on_error=True)

        if(mos.speakerKeeperState != 0x01):
            print "Enabling speaker keeper..."
            (status, null) =self.__tx_dev.keep(1)
            if(status != 0x01):
                self.__device.decode_error_status(status, cmd='keep(1)', print_on_error=True)

        return True

    @config('dev_tx', [[_dirs]])
    def push_fw_file(self, filename, *slave_indices):
        """Push firmware to discovered RX.

        usage: push_fw_file <filename> [device_index...]

        example:
            Push to all connected RX devices:
                push_fw_file <rx_device_fw.nvm>

            Push to particular connected RX devices via device indices:
                push_fw_file <rx_device_fw.nvm> 1 3

        """
        slave_indices = map(int,slave_indices)
        if(not self._fw_prep(filename)):
            return

        (status, slave_count) = self.__tx_dev.slave_count()
        if(len(slave_indices) > 0):
            # Only push to given slaves
            if(max(slave_indices) > (slave_count-1)):
                logging.error("The given RX device indices are not valid. Valid indices are:")
                logging.error("".join("%d " % i for i in range(slave_count)))
                return
            else:
                self._push_fw_to_slave_indices(filename, slave_indices)
        else:
            # Push to all discovered slaves
            self._push_fw_to_slave_indices(filename, range(slave_count))

    def _push_fw_to_slave_indices(self, filename, slave_index_list):
        """Common method for pushing FW to slaves."""
        already_pushed_macs = []
        for slave_index in slave_index_list:
            current_mac = None
            # Check MACs so dual mode slaves only get pushed FW once
            (status, smd) = self.__tx_dev.get_speaker_module_descriptor(slave_index, 0)
            self.__device.decode_error_status(status, cmd='get_speaker_module_descriptor(%s)' % slave_index, print_on_error=True)
            if(status == 0x01):
                current_mac = ":".join(["%.2X" % i for i in smd.macAddress])
                if(current_mac in already_pushed_macs):
                    continue

            term_columns, sizey = terminalsize.get_terminal_size()
            id_str = "(%s) %s " % (slave_index, current_mac)
#            out_str = '=={}{:=^{width}}'.format(id_str,"",width=term_columns-len(id_str))
            out_str = '{}{:=^{width}}'.format(id_str,"",width=term_columns-len(id_str))
            print out_str

            (status, null) = self.__device.load_fw_from_file(filename, slave_index)
            if(status == 0x01):
                print("success")
                print("waiting for reboot...")
                time.sleep(6)

                (status, smd) = self.__tx_dev.get_speaker_module_descriptor(slave_index, 1)
                self.__device.decode_error_status(status, cmd='get_speaker_module_descriptor(%s)' % slave_index, print_on_error=True)

                # Add MACs to already_pushed_macs list
                if((status == 0x01) and current_mac):
                    already_pushed_macs.append(current_mac)
            else:
                self.__device.decode_error_status(status, cmd='load_fw_from_file(%s)' % filename, print_on_error=True)

    @config('dev_all', [[_dirs]])
    def load_fw_file(self, filename):
        """Load firmware onto devices.

        usage: [[MAC].]load_fw_file <filename>

        example:
            Load TX device firmware:
                > load_fw <tx_device_fw.nvm>

            Load firmware to all serially connected RX devices:
                > .load_fw <rx_device_fw.nvm>

            Load firmware to specific serially connected RX device:
                > 02:EA:00:00:00:01.load_fw <rx_device_fw.nvm>

        """
        if(os.path.exists(filename)):
            print("Loading %s ..." % filename)
        else:
            print("No such file: %s" % filename)
            return False

        term_columns, sizey = terminalsize.get_terminal_size()
        out_str = '== {} {:=^{width}}'.format(self.__device['mac'],"",width=term_columns-21)
        print out_str
        (status, null) = self.__device.load_fw_from_file(filename)
        if(status == 0x01):
            print("success")
            print("Waiting for reboot...")
            time.sleep(5)
        else:
            self.__device.decode_error_status(status, cmd='load_fw_from_file(%s)' % filename, print_on_error=True)

    @config('dev_all')
    def load_fw_from_eeprom(self):
        """Load the image from an extern EEProm to the module

        usage: [[MAC].]load_fw_from_eeprom
        """
        print "Loading Summit module from EEProm image..."
        (status, null) = self.__device.load_fw_from_eeprom()
        self.__device.decode_error_status(
                status,
                'load_fw_from_eeprom',
                print_on_error=True)

    @config('dev_all')
    def load_eeprom_from_fw(self):
        """Load the current active FW image into an external EEProm.

        usage: [[MAC].]load_eeprom_from_fw
        """
        print "Loading EEProm..."
        (status, null) = self.__device.load_fw_to_eeprom()
        self.__device.decode_error_status(
                status,
                'load_fw_to_eeprom',
                print_on_error=True)

    @config('restr_all')
    def uptime(self):
        """Print how long the system has been running.

        usage: [[MAC].]uptime

        """
        (status, value) = self.__device.get_time_info()
        self.__device.decode_error_status(status, cmd='uptime', print_on_error=True)

        x = datetime.timedelta(seconds=value.uptime)
        print "%s - up %s" % (self.__device.get_our_mac()[1], x)

    @config('restr_all')
    def syslog(self):
        """Print the system log for a device.

        usage: [[MAC].]syslog

        """

        (status, value) = self.__device.get_time_info()
        self.__device.decode_error_status(status, cmd='uptime', print_on_error=True)

        boottime = datetime.datetime.now(PST())
        boottime = boottime - datetime.timedelta(seconds=value.uptime)
        print("boot time = %s" % boottime.time())

        while True:
            (status, buffer) = self.__device.get_syslog_data()
            self.__device.decode_error_status(status, cmd='syslog', print_on_error=True)

            for i in range(buffer[1]):
                x = boottime + datetime.timedelta(milliseconds=buffer[0].syslogentries[i].time)
                print ( "%s> %s" % (x.strftime("%I:%M:%S"), buffer[0].syslogentries[i]))
            if(buffer[1] < desc.MAX_NUMBER_SYSLOG_ENTRIES):
                break

    @config('restr_all')
    def netstat(self, reset='0'):
        """Print some network statistics.

        usage: [[MAC].]netstat [0|1]

        """
        (status, value) = self.__device.netstat(int(reset,0))
        self.__device.decode_error_status(status, cmd='netstat', print_on_error=True)
        if(reset == '0'):
            print "== %s ==========================================================" % self.__device['mac']
            print value

    @config('dev_all')
    def erase_flash(self):
        """Erase the entire contents of flash. BE CAREFUL!!

        usage: [[MAC].]erase_flash

        """
        (status, value) = self.__device.erase_flash()
        self.__device.decode_error_status(status, cmd='erase_flash', print_on_error=True)


    @config('dev_all')
    def mfg_dump(self, prefix=None):
        """Dump the manufacturing data to a file.

        usage: [[MAC].]mfg_dump [prefix]

        The auto generated filename will contain the MAC address of the device.
            02-EA-00-00-00-01_mfg.txt

        If an optional prefix is given it will be prepended to the filename:
            > mfg_dump foo
            foo_02-EA-00-00-00-01_mfg.txt

        """
        if(prefix):
            pre = prefix + "_"
        else:
            pre = ""
        filename = pre + self.__device['mac'] + "_mfg.txt"
        filename = re.sub(':','-',filename)

        if(os.path.exists(filename)):
            overwrite = raw_input("%s exists. Overwrite it? [y,n] " % filename)
            if(overwrite.lower() != "y"):
                return

        print separator(self.__device['mac'])
        print "writing mfg data to %s..." % filename
        (status, null) = self.__device.mfg_dump(filename)
        if(status == 0x01):
            print "success"
        else:
            print self.__device.decode_error_status(status, 'mfg_dump(%s)' % filename)

    @config('dev_all', [[_dirs]])
    def mfg_load(self, filename, force=None):
        """Load a manufacturing file.

        usage: [[MAC].]mfg_load <filename> [force]

        The command won't allow an MFG file to be loaded if the MAC address
        of the device doesn't match the MAC address contained in the file.
        Adding the "force" option will override this check, so be careful.

        """
        if(not os.path.exists(filename)):
            print "%s doesn't exist" % filename
            return

        with open(filename, 'r') as file:
            for line_no in range(12): # Read the 12 line
                mac_line = file.readline()
        mac_line = mac_line.split()
        if(mac_line[-1] == "MacAddress"):
            updated_mac_line = ["%.2x" % int(x,16) for x in mac_line[:6]]
            mac = ":".join(updated_mac_line).upper()
        else:
            logging.error("%s looks like an invalid MFG data file!" % filename)
            logging.error("Could not find the MacAddress entry at line 12")
            return

        print separator(self.__device['mac'])
        if(mac != self.__device['mac']):
            logging.error("The MAC in the file doesn't match the MAC of the device")
            err_str = "    {:17}  {:17}".format("File", "Device")
            logging.error(err_str)
            err_str = "    {:17}  {:17}\n".format(mac, self.__device['mac'])
            logging.error(err_str)
            if(force == 'force'):
                logging.warning("FORCING INVALID MFG FILE AT YOUR REQUEST!!!")
            else:
                logging.error("Use the 'force' option to write this file anyway")
                return

        (status, null) = self.__device.mfg_load(filename)
        if(status == 0x01):
            print "success"
        else:
            print self.__device.decode_error_status(status, 'mfg_load(%s)' % filename)

    @config('dev_all')
    def flash_read(self, address, num_bytes):
        """Get num_bytes from flash.

        usage: [[MAC].]flash_read <address> <num_bytes>

        """
        address = int(address, 0)
        num_bytes = int(num_bytes, 0)
        (status, buf) = self.__device.get_flash_data(address, num_bytes)
        print ""
        if(status == 0x01):
            print utils.pretty_print_bytes(buf)
        else:
            print self.__device.decode_error_status(status)

    @config('dev_rx', [[_dirs]])
    def coef_load(self, filename):
        """Load a coefficient 'view' file into flash.

        usage: [MAC].coef_load <filename>

        """
        if(not os.path.exists(filename)):
            print "%s doesn't exist" % filename
            return

        coef_ds = fs.FLASH_COEFFICIENT_SECTION_104()
        coef_ds.read(filename)
        (status, null) = self.__device.set_coefficient_data(coef_ds)
        if(status == 0x01):
            print "success"
        else:
            print self.__device.decode_error_status(status)

    @config('dev_rx', [[_dirs]])
    def coef_dump(self, prefix=None):
        """Dump a coefficient 'view' file.

        usage: [MAC].coef_dump [prefix]

        """
        if(prefix):
            pre = prefix + "_"
        else:
            pre = ""
        filename = pre + self.__device['mac'] + "_coef.txt"
        filename = re.sub(':','-',filename)

        if(os.path.exists(filename)):
            overwrite = raw_input("%s exists. Overwrite it? [y,n] " % filename)
            if(overwrite.lower() != "y"):
                return

        print separator(self.__device['mac'])
        (status, coef_ds) = self.__device.get_coefficient_data()
        if(status == 0x01):
            coef_ds.write(filename)
            print "success"
        else:
            print self.__device.decode_error_status(status)

    @config('restr_all')
    def tx(self, packet_count):
        """Transmit a particular number of packets.

        usage: [[MAC].]tx <number_of_packets_to_send>

        """
        (status, null) = self.__device.transmit_packets(int(packet_count,0))
        if(status != 0x01):
            print self.__device.decode_error_status(status)

    @config('restr_all', [['reset']])
    def rx(self, reset=None):
        """Print or reset packet reception statistics.

        usage: [[MAC].]rx [reset]

        If the "reset" option is given the rx statistics are reset.

        """
        if(reset == 'reset'):
            (status, stats) = self.__device.reset_rx_statistics()
            if(status != 0x01):
                print self.__device.decode_error_status(status)
        else:
            (status, stats) = self.__device.receive_statistics()
            if(status == 0x01):
                return "%d" % (stats.totalPacketCount)
            else:
                print self.__device.decode_error_status(status)

    @config('restr_all', [map(str, range(25))])
    def set_transmit_power(self, power):
        """Set the transmit power level (dBm)

        usage: [[MAC].]set_transmit_power <power>

        example:
            > set_transmit_power 15

        """
        (status, null) = self.__device.set_transmit_power(int(power,0))
        if(status != 0x01):
            print self.__device.decode_error_status(status)

    @config('restr_all')
    def get_transmit_power(self):
        """Returns the current transmit power level (dBm).

        usage: [[MAC].]get_transmit_power

        """
        (status, power) = self.__device.get_transmit_power()
        self.__device.decode_error_status(status, cmd='get_transmit_power', print_on_error=True)
        return "%d dBm" % (power)

    @config('restr_all', [['working','monitor'], map(str, range(35))])
    def set_radio_channel(self, radio, channel):
        """Set the working or monitor radios to specific channels

        usage: [[MAC].]set_radio_channel <working|monitor> <channel>

        example:
            > set_radio_channel working 7
            > set_radio_channel monitor 19

        """
        if (radio == 'working') or (radio == '0'):
            (status, null) = self.__device.set_radio_channel(0, int(channel,0))
            if(status != 0x01):
                print self.__device.decode_error_status(status)
        elif (radio == 'monitor') or (radio == '1'):
            (status, null) = self.__device.set_radio_channel(1, int(channel,0))
            if(status != 0x01):
                print self.__device.decode_error_status(status)
        else:
            print(self.help('set_radio_channel'))

    @config('restr_all')
    def temp(self):
        """Print the temperature of the device in degrees Celsius.

        usage: [[MAC].]temp

        """
        (status, temp_celcius) = self.__device.temperature()
        st = "%d°C" % temp_celcius
        return st

    @config('restr_tx', [['restore','new','add','remove'], ['trace']])
    def wizard(self, mode = 'restore', trace = ''):
        """System Startup Wizard

        Interactively prompts user for system configuration decisions and sets up a
        Summit SWM908 Development Kit based audio network using the supported Summit
        system sequences.

        Usage:
          wizard [mode] [trace]

        Arguments:
          If none specified, wizard defaults to restore mode, no trace

          mode  -- new      initializes, configures and saves system state
                   add      add slaves to currently saved state
                   remove   removes slaves from currently saved state
                   restore  brings up system from previously saved state

          trace -- enables API call trace and sequence ID display

        Other Info:
          The user's answers to prompts are not case sensitive and must be fully formed.
          Only valid choices are accepted and there are no default values.

          Speakers are identified by their MAC address (should be labeled as such).
          A tone is output from each speaker during location assignment to assist
          with identification.

          The Summit devkit supports 8 channel digital audio from the direct I2S port
          on the Glenwood bridge or as analog input from the optional Redmond board.
          The wizard's input source selection step configures the I2S clock source
          relative to the master: IN for i2s input or OUT for analog input via Redmond.

          Output volume is dependant on the source input level and volume setting.
          A typical volume setting with line level input to the Redmond board is 70 %

          A test profile (wizard.cfg) representing the configured system state
          is written to the users home directory overwriting any previous file.
          Copy this file to a different name if you wish to preserve it.
        """

        wiz = Wizard(self, self.__tx_dev)
        wiz.wizard(mode, trace)

    @config('restr_all', [['9000'], ['32']])
    def get_pdout(self, delay, sample_count):
        """Sample the power detect output and return its value.

        usage: [[MAC].]get_pdout <delay> <sample_count>

        """
        delay = int(delay,0)
        sample_count = int(sample_count,0)

        (status, null) = self.__device.set_power_comp_enable(0)
        if(status != 0x01):
            print self.__device.decode_error_status(status, cmd="set_power_comp_enable(0)")

        (status, pdout) = self.__device.get_pdout(delay, sample_count)
        if(status != 0x01):
            print self.__device.decode_error_status(status)
        else:
            print "0x%X" % pdout

        (status, null) = self.__device.set_power_comp_enable(1)
        if(status != 0x01):
            print self.__device.decode_error_status(status, cmd="set_power_comp_enable(1)")

def main():
    debug_levels = {'debug': logging.DEBUG,
                    'info': logging.INFO,
                    'warning': logging.WARNING,
                    'error': logging.ERROR,
                    'critical': logging.CRITICAL
    }
    parser = argparse.ArgumentParser(description='RA Command Monitor',
    formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=32))
    parser.add_argument('--debug', dest='debug', choices=['debug', 'info', 'warning', 'error', 'critical'], default='warning')
#    parser.add_argument('--list-tests', action='store_true', dest='list_tests')
    parser.add_argument('-t', '--run-test', action='append', dest='tests')
    parser.add_argument('-p', '--profile', dest='profile')
    parser.add_argument('-i', '--iterations', dest='iterations', default=1)
#    parser.add_argument('--no-log', dest='serial_logging', action='store_false', help="disable serial logging")
    parser.add_argument('--no-db', action='store_true', dest='no_db')
    group = parser.add_argument_group('Tx interface selection')
    group.add_argument('--interface', dest='tx_interface', choices=['i2c','usb','uart'],
      default='i2c', help='i2c | usb [VendorId, ProductID] | uart <port_or_url>')
    group.add_argument('param1', nargs='?',
      help='VendorID for usb, port or URL for uart\ne.g.  0x2495    or    /dev/ttyUSB3')
    group.add_argument('param2', nargs='?', help='ProductID for usb\ne.g.  0x0016')
    parser.add_argument('--rp', '--rx-uart-port', dest='rx_uart_ports', action='append', help='specific RX port(s) to use.')
    parser.add_argument('--dut-pwr', dest='dut_pwr', action='store_true')
#    parser.add_argument('--sqlite', action='sqlite_file')
    args = parser.parse_args()

#    logging.basicConfig(filename='cmd.log', level=logging.DEBUG)
#    logging.basicConfig(level=debug_levels[args.debug])
    console = ansistrm.ColorizingStreamHandler()
#    console.setLevel(debug_levels[args.debug])
    logging.getLogger().setLevel(debug_levels[args.debug])
    formatter = logging.Formatter('%(message)s')
    console.setFormatter(formatter)
    logging.getLogger().addHandler(console)

    if(args.tests):
        CMD = RAConsole(
            logging_level=debug_levels[args.debug],
            interactive=False,
            tx_interface=args.tx_interface,
            tx_param1=args.param1,
            tx_param2=args.param2,
            dut_pwr=args.dut_pwr)
        if(args.profile):
            CMD.load_test_profile(args.profile)
        for test in args.tests:
            if(args.no_db):
                CMD.unset('db')
            try:
                CMD.run(test, args.iterations)
            except:
                raise
            finally:
                CMD.cleanup()
    else:
        CMD = RAConsole(logging_level=debug_levels[args.debug],
                        tx_interface=args.tx_interface,
                        tx_param1=args.param1,
                        tx_param2=args.param2,
                        rx_uart_ports=args.rx_uart_ports,
                        dut_pwr=args.dut_pwr)
        if(args.profile):
            CMD.load_test_profile(args.profile)
        CMD.collect_devs()
#        if(args.serial_logging):
#            CMD.log('1')
#        else:
#            print("<<< Serial port logging is disabled >>>")

        CMD.cmdloop()
    logging.shutdown()

if __name__ == '__main__':
    main()
