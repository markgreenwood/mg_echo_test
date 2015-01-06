from __future__ import division

import logging
from pysummit import comport
from pysummit import descriptors as desc
from pysummit import decoders as dec
from pysummit.devices import TxAPI
from pysummit.devices import RxAPI
from pysummit.power_controller import PowerController
from pysummit import testprofile

filename = "MG_echo_data.csv"

def main(TX, RX, tp=None, pc=None, args=[]):
    with open(filename, 'a') as f:
        f.write("------------------\niteration,tx,rx\n")
        f.flush()
        echo_attempts = 1000
        if (args):
            iterations = int(args[0])
        else:
            iterations = 1
        for iteration in range(iterations):
            (status, null) = TX.keep(0)
            if(status != 0x01):
                print TX.decode_error_status(status, "keep(0)")

            # Reset master statistics
            (status, null) = TX.netstat(1)
            if(status != 0x01):
                print TX.decode_error_status(status, "netstat(1)")

            # Reset all slave statistics
            for rx in RX:
                (status, null) = rx.netstat(1)
                if(status != 0x01):
                    print rx.decode_error_status(status, "netstat(1)")

            # Echo to slave index 0
            for echo_count in range(echo_attempts):
                print "Echo #", echo_count
                (status, null) = TX.echo(0, retry=1)
                if(status != 0x01):
                    print dec.decode_error_status(status, "echo(0, retry=1)")

            # Get netstat from master
            (status, ns_struct) = TX.netstat(0)
            if(status != 0x01):
                print TX.decode_error_status(status, "netstat(0)")

            tx_totalPackets = 0
            for i in range(4):
                tx_totalPackets  += ns_struct.PacketReceiveErrors[i]

            print "TX: Packets Received:", tx_totalPackets

            # Get netstat from slaves
            for rx in RX:
                (status, ns_struct) = rx.netstat(0)
                if(status != 0x01):
                    print rx.decode_error_status(status, "netstat(0)")

                rx_totalPackets = 0
                for i in range(4):
                    rx_totalPackets  += ns_struct.PacketReceiveErrors[i]


                print "RX: Packets Received:", rx_totalPackets

            f.write('%d,%d,%d\n' % (iteration, tx_totalPackets, rx_totalPackets))
            f.flush()

            print "echo_attempts: ", echo_attempts, "\n"
            print "rx_totalPackets: ", rx_totalPackets, "\n"
            print "tx_totalPackets: ", tx_totalPackets, "\n"
            print "TxPER: ", 100.*(1.-(float(rx_totalPackets)/echo_attempts)), "%\n"
            print "RxPER: ", 100.*(1.-(float(tx_totalPackets)/rx_totalPackets)), "%\n"

#            print "TEST CASE ======"
#            rx_totalPackets = 995
#            tx_totalPackets = 983
#            print "echo_attempts: ", echo_attempts, "\n"
#            print "rx_totalPackets: ", rx_totalPackets, "\n"
#            print "tx_totalPackets: ", tx_totalPackets, "\n"
#            print "TxPER: ", 100.*(1.-(rx_totalPackets/echo_attempts)), "%\n"
#            print "RxPER: ", 100.*(1.-(tx_totalPackets/rx_totalPackets)), "%\n"

            if((iterations > 1) and (iteration+1 < iterations)):
                a = raw_input("  Press return to continue...")

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
    Tx = TxAPI()
    Rx = RxAPI()
    coms = []
    ports = comport.ComPort.get_open_ports()
    print "ports = ", ports, "\n"
    for port in ports:
        coms.append(comport.ComPort(port))
    print "coms = ", coms, "\n"
    Rx.set_coms(coms, prune_devs=1)

# Start the test
    main(Tx, Rx)
