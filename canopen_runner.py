#!/usr/bin/env python
# @brief Configure CANopen Object Dictionary using a CSV file
# @param(string,can0) CAN Device Name
# @param(int,1) CAN ID
# @param(string,puck3.eds) Electronic Data Sheet
# @param(string,../conf/canopen_ecmax_22.csv) Configuration file

import csv
#import click
import enum
import time
import ast
import operator as op
import logging
#from . import constants as const
import canopen
import platform
import sys
from canopen.objectdictionary import import_od
from canopen.sdo import SdoCommunicationError, SdoAbortedError
from struct import pack, unpack

logger = logging.getLogger(__name__)
verbose = False
node = 0 # Global CANopen node structure

###############################################################################
################################ ENUMERATIONS #################################
###############################################################################

class COMMAND(enum.Enum):
    """
    Enumeration of all commands specified in CANOpen player
    (/docs/CANopenFilePlayerManual.pdf) Appendix A
    """
    FILE_INFO = "FILE_INFO"
    FILE_VERSION = "FILE_VERSION"
    SETTING_NODE_ID = "SETTING_NODE_ID"
    SETTING_NODE_ID_ADD = "SETTING_NODE_ID_ADD"
    SETTING_B2B_TIMEOUT = "SETTING_B2B_TIMEOUT"
    SETTING_RETRIES = "SETTING_RETRIES"
    SETTING_SDO_TIMEOUT = "SETTING_SDO_TIMEOUT"
    CONTROL_PAUSE = "CONTROL_PAUSE"
    CONTROL_WAIT_FOR = "CONTROL_WAIT_FOR" #unimplemented
    CONTROL_NMT = "CONTROL_NMT"
    CONTROL_LSSM = "CONTROL_LSSM" #unimplemented
    CONTROL_SDO_READ = "CONTROL_SDO_READ" #unimplemented
    CONTROL_SDO_BUFFER = "CONTROL_SDO_BUFFER" #unimplemented

    @classmethod
    def contains(cls, value):
        """
        Returns true if value string is a valid command, false otherwise
        """
        return (any(value == item.value for item in cls))


class DATATYPE(enum.Enum):
    """
    Enumeration of all datatypes specified in CANOpen player
    (/docs/CANopenFilePlayerManual.pdf) Appendix A
    """
    UNSIGNED8 = "UNSIGNED8"
    INTEGER8 = "INTEGER8"
    INTEGER16 = "INTEGER16"
    INTEGER32 = "INTEGER32"
    UNSIGNED16 = "UNSIGNED16"
    UNSIGNED24 = "UNSIGNED24"
    UNSIGNED32 = "UNSIGNED32"
    UNSIGNED40 = "UNSIGNED40"
    UNSIGNED48 = "UNSIGNED48"
    UNSIGNED56 = "UNSIGNED56"
    UNSIGNED64 = "UNSIGNED64"
    REAL32 = "REAL32"
    STRING = "STRING"
    DOMAIN = "DOMAIN" #unimplemented
    LSSMASTERRECORD = "LSSMASTERRECORD" #unimplemented

    @classmethod
    def contains(cls, value):
        """
        Returns true if value string is a valid datatype, false otherwise
        """
        return (any(value == item.value for item in cls))

    @classmethod
    def isNumber(cls, value):
        """
        Returns true if datatype is valid and is an INTEGER8 or UNSIGNED type
        """
        if any(value == item.value for item in cls):
            return value[0:8] == "UNSIGNED" or value[0:7] == "INTEGER"

    @classmethod
    def isFloat(cls, value):
        if any(value == item.value for item in cls):
            return value[0:4] == "REAL"

###############################################################################
############################# UTILITY FUNCTIONS ###############################
###############################################################################

def progressbar(update_progress):
    progress = 0
    while progress < 100:
        progress += 1
        time.sleep(0.1)
        update_progress.put(progress)
    update_progress.put("Done")

def printout(text, override=False):
    """
    Prints the text if global verbose is true or override is true
    """
    if verbose or override:
        print(text)
        #click.echo(text)

def parse_error_code(code):
    """
    Returns a string description of the provided error code
    Descriptions are specified for the runner command
    """
    global errors 

    if code != 0:
        errors = errors + 1

    if code == 0:
        return "No Error"
    elif code == 1:
        return "Invalid line format - invalid number of tokens (6 expected)"
    elif code == 2:
        return "No command specified and either index or subindex is empty"
    elif code == 3:
        return "Specified command not recognized"
    elif code == 4:
        return "Command specified and either index or subindex is nonempty"
    elif code == 5:
        return "Index or subindex not a valid number"
    elif code == 6:
        return "Invalid datatype specified"
    elif code == 8:
        return "Datatype does not match expected datatype for the given command"
    elif code == 9:
        return "No SDO response"
    elif code == 10:
        return ("Data overflow, data not valid for given datatype, or use of "
               "$ID without specifying a desired ID")
    elif code == 11:
        return ("Key not found, specified index/subindex pair not found in EDS "
               "object dictionary")
    elif code == 12:
        return "Datatype not implemented"
    elif code == 13:
        return "Command not implemented"
    else:
        return "Unknown error"
    return None

def parse_int(val):
    """
    Attempt to parse string val into int (determining the most appropriate base)
    If there is an error (ie val is not valid numeric string), None is returned
    """
    try:
        return int(val, 0)
    except:
        return None

def parse_data(datatype, data, can_id=-1):
    """
    Returns a tuple size 2. The first element is the data (an array of bytes,
    in the case of an int, or a string if the datatype is non-numeric). The
    second element is the integer representation of the data if applicable.

    Every instance of the substring '$ID' in data is replaced by can_id. This
    holds for both integer data as well as string data.
    """
    try:
        if "$ID" in data and can_id == -1:
            print("Failed on $ID")
            return None
        data = data.replace("$ID", str(can_id))
        if not (DATATYPE.isNumber(datatype) or DATATYPE.isFloat(datatype)):
            #print("Failed on DATATYPE")
            return (data, None)

        data = math_eval(ast.parse(data, mode='eval').body)
        #print("after math_eval")
        #print("data: {0}".format(data))
        if datatype == DATATYPE.INTEGER8.value:
            #print("INTEGER8")
            byte_array = data.to_bytes(1, 'little', signed=True)
        elif datatype == DATATYPE.INTEGER16.value:
            #print("INTEGER16")
            byte_array = data.to_bytes(2, 'little', signed=True)
        elif datatype == DATATYPE.INTEGER32.value:
            #print("INTEGER32")
            byte_array = data.to_bytes(4, 'little', signed=True)
        elif datatype == DATATYPE.UNSIGNED8.value:
            #print("UNSIGNED8")
            byte_array = data.to_bytes(1, 'little', signed=False)
        elif datatype == DATATYPE.UNSIGNED16.value:
            #print("UNSIGNED16")
            byte_array = data.to_bytes(2, 'little', signed=False)
        elif datatype == DATATYPE.UNSIGNED32.value:
            #print("UNSIGNED32")
            byte_array = data.to_bytes(4, 'little', signed=False)
        elif datatype == DATATYPE.REAL32.value:
            #print("REAL32")
            byte_array = bytes(pack("<f", float(data)))
        else:
            #print("before data.to_bytes")
            byte_array = data.to_bytes(int(int(datatype[8:10]) / 8), 'little')
            #print("after data.to_bytes")
    except Exception as e:
        logger.critical(e)
        print("Failed on Exception")
        return None
    return (byte_array, data)



def math_eval(node):
    """
    Evaluates an ast expression
    """
    operators = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
        ast.Div: op.truediv, ast.Pow: op.pow, ast.USub: op.neg,
        ast.BitXor: op.xor, ast.BitAnd: op.and_, ast.BitOr: op.or_,
        ast.Invert: op.inv, ast.LShift: op.lshift, ast.RShift: op.rshift,
        ast.Eq: op.eq, ast.Lt: op.lt, ast.Gt: op.gt, ast.LtE: op.le,
        ast.GtE: op.ge, ast.NotEq: op.ne}

    if isinstance(node, ast.Constant): # <number>
        return node.value
    elif isinstance(node, ast.BinOp): # <left> <operator> <right>
        return operators[type(node.op)](math_eval(node.left),
                                        math_eval(node.right))
    elif isinstance(node, ast.UnaryOp): # <operator> <operand> e.g., -1
        return operators[type(node.op)](math_eval(node.operand))
    elif isinstance(node, ast.Compare): # <operator> <operand> <operator>
        return 1 if operators[type(node.ops[0])](math_eval(node.left),
                                math_eval(node.comparators[0])) == True else 0
    else:
        raise TypeError(node)

###############################################################################
############################### MAIN FUNCTIONS ################################
###############################################################################

def canopen_runner(csvfile, replace_id, start_id, edsfile, v, force,
                   no_warnings):
    """
    Main function for Runner, called by cli.py
    First validates the provided CSV file, then conditionally launches runner
    :param replace_id: number with which to replace all instances of '$ID'
                       in the data column of the CSV
    :param start_id: the initial CAN id to use
    :param edsfile: object dictionary file reference
    :param v: enable verbose mode
    :param force: proceed with runner even if validation fails
    :param no_warnings: do not print validation/execution warnings to the screen
    """
    global verbose
    verbose = v
    #print("replace_id: {0}".format(replace_id))
    objdict = None
    errors = 0

    if edsfile != None:
        try:
            objdict = import_od(edsfile)
        except:
            printout("Error: unable to parse EDS file", True)
            return

    (invalid_lines, warning_lines) = validate(csv.reader(csvfile), replace_id, objdict)
    if not no_warnings:
        for line in warning_lines:
            display_line = ("Warning (line " + str(line[0]) + "): " +
                            parse_error_code(line[1]))
            printout(display_line, True)
    for line in invalid_lines:
        display_line = ("Error (line " + str(line[0]) + "): " +
                        parse_error_code(line[1]))
        printout(display_line, True)
        # errors = errors + 1

    #if there are invalid lines & user has elected not to force continue, quit
    if len(invalid_lines) and not force:
        return
    csvfile.seek(0) #reset CSV file pointer
    result = execute_canopen_runner(csv.reader(csvfile), replace_id, start_id)
    # print('result of run = {}'.format(result))

    return errors

def validate(csvfile, replace_id, objdict):
    """
    Validates the CSVfile according to the format specified in the
    CANopenFilePlayerManual.pdf in the /docs/ folder.
    Returns a tuple of size 2. The first element is a list of (linenum,errornum)
    pairs, and the second element is a list of (linenum, warningnum) pairs.
    """
    invalid_lines = []
    warning_lines = []
    
    linenum = 0
    for row in csvfile:
        linenum += 1
        #print("Row:{0}, Fields:{1}, Data:[{2}]".format(linenum, len(row), row))

        if len(row) == 0 or (len(row) == 1 and row[0].isspace()): #is empty line
            continue #skip it
        if row[0][0] == '#': # Is a comment
            #print("Comment: {0}".format(row[0]))
            continue
        if len(row) != 6:
            invalid_lines.append((linenum, 1))
            continue

        command = row[1].strip()
        index = row[2].strip()
        subindex = row[3].strip()
        datatype = row[4].strip()
        data = row[5].strip()
        if command == '':
            if index == '' or subindex == '':
                invalid_lines.append((linenum, 2))
                continue
            a = parse_int(index)
            b = parse_int(subindex)
            if a == None or b == None:
                invalid_lines.append((linenum, 5))
                continue
            if objdict != None:
                try:
                    test = objdict[a]
                    if b:
                        test = test[b]
                except KeyError:
                    warning_lines.append((linenum, 11))
                    continue
        if command != '':
            #command exists, but index or subindex nonempty
            if index != '' or subindex != '':
                invalid_lines.append((linenum, 4))
                continue
            if not COMMAND.contains(command): #not valid command
                invalid_lines.append((linenum, 3))
                continue
        if not DATATYPE.contains(datatype): #not valid datatype
            invalid_lines.append((linenum, 6))
            continue

        #CHECKING THAT COMMANDS MATCH THEIR DATATYPES
        if (command == COMMAND.FILE_INFO.value and
                datatype != DATATYPE.STRING.value):
            invalid_lines.append((linenum, 8))
            continue
        if (command == COMMAND.FILE_VERSION.value and
                datatype != DATATYPE.STRING.value):
            invalid_lines.append((linenum, 8))
            continue
        if (command == COMMAND.SETTING_B2B_TIMEOUT.value and
                datatype != DATATYPE.UNSIGNED16.value):
            invalid_lines.append((linenum, 8))
            continue
        if (command == COMMAND.SETTING_NODE_ID.value and
                datatype != DATATYPE.UNSIGNED8.value):
            invalid_lines.append((linenum, 8))
            continue
        if (command == COMMAND.SETTING_NODE_ID_ADD.value and
                datatype != DATATYPE.INTEGER8.value):
            invalid_lines.append((linenum, 8))
            continue
        if (command == COMMAND.SETTING_RETRIES.value and
                datatype != DATATYPE.UNSIGNED8.value):
            invalid_lines.append((linenum, 8))
            continue
        if (command == COMMAND.SETTING_SDO_TIMEOUT.value and
                datatype != DATATYPE.UNSIGNED16.value):
            invalid_lines.append((linenum, 8))
            continue
        if (command == COMMAND.CONTROL_PAUSE.value and
                datatype != DATATYPE.UNSIGNED16.value):
            invalid_lines.append((linenum, 8))
            continue
        if (command == COMMAND.CONTROL_WAIT_FOR.value and
                datatype != DATATYPE.UNSIGNED8.value):
            invalid_lines.append((linenum, 8))
            continue
        if (command == COMMAND.CONTROL_SDO_READ.value and
                datatype != DATATYPE.UNSIGNED8.value):
            invalid_lines.append((linenum, 8))
            continue
        if (command == COMMAND.CONTROL_NMT.value and
                datatype != DATATYPE.UNSIGNED16.value):
            invalid_lines.append((linenum, 8))
            continue
        if (command == COMMAND.CONTROL_LSSM.value and
                datatype != DATATYPE.LSSMASTERRECORD.value):
            invalid_lines.append((linenum, 8))
            continue

        if parse_data(datatype, data, replace_id) == None:
            invalid_lines.append((linenum, 10))
            continue

        #WARNINGS-UNIMPLEMENTED #TODO
        if datatype == DATATYPE.LSSMASTERRECORD.value:
            warning_lines.append((linenum, 12))
        if datatype == DATATYPE.DOMAIN.value:
            warning_lines.append((linenum, 12))
        if command == COMMAND.CONTROL_WAIT_FOR.value:
            warning_lines.append((linenum, 13))
        if command == COMMAND.CONTROL_LSSM.value:
            warning_lines.append((linenum, 13))
        if command == COMMAND.CONTROL_SDO_BUFFER.value:
            warning_lines.append((linenum, 13))
        if command == COMMAND.CONTROL_SDO_READ.value:
            warning_lines.append((linenum, 13))

    return (invalid_lines, warning_lines)


def execute_canopen_runner(csvfile, replace_id, start_id):
    """
    Actually parses the csv file and sends the proper sequence of CANOpen
    SDO/NMT messages as well as accurately processes other commands as specified
    in the CANOpenFilePlayer manual. Running in verbose mode causes progress
    messages to be printed to the console, otherwise this function will run
    silently until an error occurs.
    """
    printout("Starting runner...")

    can_id = start_id
    sdo_timeout = 1 #pulled from canopen.sdo
    sdo_retries = 1 #pulled from canopen.sdo
    sdo_b2b = 1 #set to 0 as default
    global node
    global errors
    
    linenum = 0
    for row in csvfile:
        linenum += 1
        if len(row) == 0 or (len(row) == 1 and row[0].isspace()): #is empty line
            continue #skip it
        if row[0][0] == '#': # Is a comment
            #print("Comment: {0}".format(row[0]))
            continue
        comment = row[0].strip()
        command = row[1].strip()
        index = row[2].strip()
        subindex = row[3].strip()
        datatype = row[4].strip()
        datastr = row[5].strip()
        (data, dataint) = parse_data(datatype, datastr, replace_id)
        if command == COMMAND.FILE_INFO.value:
            printout("File Information: " + data)
        elif command == COMMAND.FILE_VERSION.value:
            printout("File Version: " + data)
        elif command == COMMAND.SETTING_NODE_ID.value:
            printout("Setting node id to " + str(dataint) + ". Comment: " +
                     comment)
            can_id = dataint
        elif command == COMMAND.SETTING_NODE_ID_ADD.value:
            printout("Setting node id to " + str(dataint) + ". Comment: " +
                     row[0])
            can_id += dataint
        elif command == COMMAND.SETTING_B2B_TIMEOUT.value:
            printout("Setting SDO B2B time to " + str(dataint) + "ms. Comment: "
                     + row[0])
            sdo_b2b = dataint
        elif command == COMMAND.SETTING_SDO_TIMEOUT.value:
            printout("Setting SDO timeout to " + str(dataint) +
                     "ms. Comment: " + row[0])
            sdo_timeout = dataint / 1000.0
        elif command == COMMAND.SETTING_RETRIES.value:
            #must add 1 b/c can_node.sdo.MAX_RETRIES is really total attempts
            printout("Setting SDO retries to " + str(dataint + 1) +
                     ". Comment: " + row[0])
            sdo_retries = dataint + 1

        elif command == COMMAND.CONTROL_PAUSE.value:
            printout("Pausing for " + str(dataint/1000.0) + " seconds. Commend:"
                     + row[0])
            time.sleep(dataint/1000.0)
        elif command == COMMAND.CONTROL_WAIT_FOR.value:
            pass
        elif command == COMMAND.CONTROL_NMT.value:
            printout("Sending NMT Message to " + str(data[1]) + ", value " +
                     str(hex(data[0])) + ". Comment: " + row[0])
            #utils.send_raw_can_msg(0, data)
        elif command == COMMAND.CONTROL_LSSM.value:
            pass
        elif command == COMMAND.CONTROL_SDO_READ.value:
            pass
        elif command == COMMAND.CONTROL_SDO_BUFFER.value:
            pass

        else: #NOT A COMMAND, MUST BE SDO DOWNLOAD
            printout("Setting index " + str(index) + ", subindex " +
                     str(subindex) + " to " + str(hex(dataint)) + ". Comment: "
                     + row[0])
            index = parse_int(index)
            subindex = parse_int(subindex)
            try:
                node.sdo.download(index,subindex,data)

                time.sleep(sdo_b2b/1000.0)
            except ConnectionError:
                printout("ERROR: Could not connect to CAN device", True)
                errors = errors + 1
            except SdoCommunicationError:
                if (index == 0x21B0 and subindex == 0): # 0x21B0 = device ID
                    continue #suppress error b/c id inside device changed
                printout("ERROR: SDO Communication Error on line " +
                         str(linenum) + ".", True)
                errors = errors + 1
            except SdoAbortedError as e:
                printout("ERROR: SDO Aborted Error on line " + str(linenum) +
                         ". Error code " + str(e.code), True)
                errors = errors + 1
    #click.secho("Done", fg="green")
    print("Done")

# $ canopen_runner <can_dev> <can_id> <eds_file> <csv_file>
def run_main():
    global node

    can_device = sys.argv[1]
    can_id = int(sys.argv[2])
    edsfile = sys.argv[3]
    csvfile = sys.argv[4]
    print("can_device={0}".format(can_device))
    print("can_id={0}".format(can_id))
    print("edsfile={0}".format(edsfile))
    print("csvfile={0}".format(csvfile))

    # Open the CAN device
    print("Establishing a new network...")
    network = canopen.Network()

    time.sleep(0.2) # Wait for any bus-off to clear

    if platform.system() == "Windows":
      network.connect(bustype='pcan', channel='PCAN_USBBUS'+str(int(can_device[-1:])+1), bitrate=1000000)
    elif platform.system() == "Linux":
      network.connect(bustype='socketcan', channel=can_device, bitrate=1000000)

    print("Connection succeeded, adding CANopen node...")
    # Add our canopen node along with its object dictionary (for parsing)
    node = network.add_node(can_id, edsfile)

    myfile = open(csvfile, 'r')
    #config_csv = csv.reader(myfile)

    canopen_runner(myfile, can_id, can_id, None, False, False, False)

def start(can_device, can_id, edsfile, csvfile):
    global node
    global errors

    errors = 0

    print("can_device={0}".format(can_device))
    print("can_id={0}".format(can_id))
    print("edsfile={0}".format(edsfile))
    print("csvfile={0}".format(csvfile))

    # Open the CAN device
    print("Establishing a new network...")
    network = canopen.Network()

    time.sleep(0.2) # Wait for any bus-off to clear

    if platform.system() == "Windows":
      network.connect(bustype='pcan', channel='PCAN_USBBUS'+str(int(can_device[-1:])+1), bitrate=1000000)
    elif platform.system() == "Linux":
      network.connect(bustype='socketcan', channel=can_device, bitrate=1000000)

    print("Connection succeeded, adding CANopen node...")
    # Add our canopen node along with its object dictionary (for parsing)
    node = network.add_node(can_id, edsfile)

      # canopen_runner(csvfile, replace_id, start_id, edsfile, v, force, no_warnings):

    myfile = open(csvfile, 'r')
    #errors = canopen_runner(myfile, can_id, can_id, None, False, False, False)
    canopen_runner(myfile, can_id, can_id, None, False, False, False)
    print("Number of errors: {}".format(errors))
    network.disconnect()
    if errors == 0:
      return True
    else:
      return False 

if __name__ == "__main__":
    run_main()