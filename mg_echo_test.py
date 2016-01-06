from __future__ import division
import sys
import logging
from pysummit import comport
from pysummit import descriptors as desc
#from pysummit import decoders as dec
from pysummit.devices import TxAPI
from pysummit.devices import RxAPI
from pysummit.power_controller import PowerController
from pysummit import testprofile

filename = "MG_echo_data.csv"

# Format required to work as a ra script
def main(TX, RX, tp=None, pc=None, args=[]):

    with open(filename, 'a') as f:
        # Data file headings
        f.write("------------------\niteration,tx,rx\n")
        f.flush()

        # Number of packets to echo
        echo_attempts = 500

        # Set the number of times to iterate the full echo test
        if (args):
            iterations = int(args[0])
        else:
            iterations = 5

        # Echo tests start here
        for iteration in range(iterations):

            # Adjust attenuation
            a = raw_input("Set next attenuation value. Hit <Enter> to continue. ")

            # Turn off SpeakerKeeper because it broadcasts extra packets that mess up echo stats(?)
            print "Turning off SpeakerKeeper..."
            (status, null) = TX.keep(0)
            if (status != 0x01):
                print "\n", TX.decode_error_status(status, "keep(0)")

            print "Resetting master net statistics..."
            # Reset master statistics
            (status, null) = TX.netstat(1)
            if (status != 0x01):
                print "\n", TX.decode_error_status(status, "netstat(1)")

            # Reset all slave statistics
            print "Resetting all slave net statistics..."
            for rx in RX:
                (status, null) = rx.netstat(1)
                if (status != 0x01):
                    print "\n", rx.decode_error_status(status, "netstat(1)")

            # Echo to slave index 0
            print "Performing echo test..."
            for echo_count in range(echo_attempts):
                sys.stdout.write(".")
                sys.stdout.flush()
                (status, null) = TX.echo(0, retry=1)
                if (status != 0x01):
                    print "\n", TX.decode_error_status(status, "echo(0, retry=1)")

            # Get netstat from master
            print "Querying net statistics from master..."
            (status, ns_struct) = TX.netstat(0)
            if (status != 0x01):
                print TX.decode_error_status(status, "netstat(0)")
            print "\nTx netstat:\n", ns_struct

            tx_totalPackets = 0
            for i in range(4):
                tx_totalPackets += ns_struct.PacketReceiveErrors[i]

            print "\nTX: Packets Received:", tx_totalPackets

            # Get netstat from slaves
            print "Querying net statistics from slaves..."
            for rx in RX:
                (status, ns_struct) = rx.netstat(0)
                if (status != 0x01):
                    print rx.decode_error_status(status, "netstat(0)")
                print "\nRx netstat:\n", ns_struct


                rx_totalPackets = 0
                for i in range(4):
                    rx_totalPackets  += ns_struct.PacketReceiveErrors[i]


                print "RX: Packets Received:", rx_totalPackets

            # Turn SpeakerKeeper back on while we change attenuation settings
            print "Turning on SpeakerKeeper..."
            (status, null) = TX.keep(1)
            if (status != 0x01):
                print "\n", TX.decode_error_status(status, "keep(1)")

            f.write('%d,%d,%d\n' % (iteration, tx_totalPackets, rx_totalPackets))
            f.flush()

            print "echo_attempts: ", echo_attempts
            print "rx_totalPackets: ", rx_totalPackets
            print "tx_totalPackets: ", tx_totalPackets
            print "TxPER: ", 100.*(1.-(float(rx_totalPackets)/echo_attempts)), "%"
            if (rx_totalPackets > 0):
                print "RxPER: ", 100.*(1.-(float(tx_totalPackets)/rx_totalPackets)), "%"

            if ((iterations > 1) and (iteration+1 < iterations)):
                a = raw_input("Do you want to continue (Y/n)? ")
            if (a and (a[0]=="N" or a[0]=="n")):
                break

if __name__ == '__main__':

	# Set up logging to a file and the console
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)-8s] %(message)s",
        filename="power_reading.log",
        filemode="w")
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter = logging.Formatter("%(name)-8s: %(levelname)-8s %(message)s")
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)

    # Dummy TestProfile and PowerController
    #tp = testprofile.TestProfile()
    #pc = PowerController()

    # Set up devices
    Tx = TxAPI() # Instantiate a master
    Rx = RxAPI() # Instantiate a collection of slaves
    coms = [] # COM ports (empty list)
    ports = comport.ComPort.get_open_ports() # Find the open COM ports
    for port in ports:
        coms.append(comport.ComPort(port)) # Add each open COM port to the coms list
    Rx.set_coms(coms, prune_devs=1) # Search the COM port list for Summit devices

    (status, null) = Tx.dfs_override(1)
    if (status != 0x01):
        print "\n", tx.decode_error_status(status, "dfs_override(1)")

    for rx in Rx:
        # Use Antenna 2 for both transmit and receive
        print "Turning off antenna diversity..."
        (status, null) = rx.wr(0x405028, 0x02)
        if (status != 0x01):
            print "\n", rx.decode_error_status(status, "wr(0x405028, 0x02)")
        print "Selecting Antenna 2..."
        (status, null) = rx.wr(0x401018, 0x13)
        if (status != 0x01):
            print "\n", rx.decode_error_status(status, "wr(0x401018, 0x13)")
        (status, value) = rx.rd(0x405028)
        if (status != 0x01):
            print "\n", rx.decode_error_status(status, "rd(0x405028)")
        print "Reg 0x405028 = %x" % value
        (status, value) = rx.rd(0x401018)
        if (status != 0x01):
            print "\n", rx.decode_error_status(status, "rd(0x401018)")
        print "Reg 0x401018 = %x" % value

    # Combined beacon and discover (disco) 
    channel = 8
    print "Disco on channel {0}...".format(channel)
    (status, null) = Tx.disco(4500,channel,0)
    if (status != 0x01):
        print "\n", Tx.decode_error_status(status, "disco(4500,channel,0)")

    # Do I need to set i2s_clocks in?
    clks = desc.AUDIO_CLOCK_SETUP()
    clks.audioSource = 0x1
    clks.audioSetup.sclkFrequency = 0x0
    clks.audioSetup.driveClks = 0x0
    clks.audioSetup.mclkFrequency = 0x3
    clks.audioSetup.mclkOutputEnable = 0x0
    (status, null) = Tx.set_i2s_clocks(clks)
    if (status != 0x01):
        print "\n", Tx.decode_error_status(status, "set_i2s_clocks(clks)")

    # Audio slot setup
    print "Assigning audio slots..."
    (status, null) = Tx.slot(0,1)
    if (status != 0x01):
        print "\n", Tx.decode_error_status(status, "slot(0,1)")

    # Set volume
    print "Setting volume..."
    (status, null) = Tx.volume(0, 0xFFFFF)
    if (status != 0x01):
        print "\n", Tx.decode_error_status(status, "volume(0, 0xFFFFF)")

    # Start the network (go into ISOCH)
    print "Starting ISOCH mode..."
    (status, null) = Tx.start()
    if (status != 0x01):
        print "\n", Tx.decode_error_status(status, "start()")
    (status, channel) = Tx.get_radio_channel()
    if (status != 0x01):
        print "\n", Tx.decode_error_status(status, "get_radio_channel()")
    a = raw_input("On channel {0}. Are you ready to start?".format(channel))
    if (a and (a[0] == "N" or a[0] == "n")):
        exit()

    # Start the test
    main(Tx, Rx)

    # Stop the network (go out of ISOCH)
    print "Stopping ISOCH..."
    (status, null) = Tx.stop()
    if (status != 0x01):
        print "\n", Tx.decode_error_status(status, "stop()")
