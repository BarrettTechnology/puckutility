# Flashloader v2
import sys       # Python Standard Library
import platform  # Python Standard Library
import os.path   # Python Standard Library
import array     # Python Standard Library
import binascii  # Python Standard Library
import canopen
import semver
import time

# Python3 incantation for enumeration supporting reverse-lookups
# return_code = enum(Success=0, Lost_Dog=1, Runaway_Pony=2)
# return_code.Lost_Dog (--> 1)
# return_code.get_string[1] (--> 'Lost_Dog')

def progressbar(update_progress, progress):
    # No sink (standalone __main__ run): silently drop the update.
    if update_progress is None:
        return
    update_progress.put(progress)

def enum(*sequential, **named):
    enums = dict(zip(sequential, range(len(sequential))), **named)
    enums['get_string'] = dict((value, key) for key, value in enums.items())
    return type('Enum', (), enums)

flash_command = enum(NO_OPERATION=0, START=1, END=2, ERASE=3, LAUNCH=4, RESET=5)

flash_result = enum(
    SUCCESS=0, FILE_NOT_FOUND=1, CANDEV_FAILED=2, RESET_FAILED=3, 
    VERSION_INCOMPATIBLE=4, EXTENSION_INVALID=5, ERASE_FAILED=6,  
    FLASH_FAILED=7, CRC_MISMATCH=8, AUTH_FAILED=9)

def get_version(vers): # Convert uint32_t to semantic version: Major.Minor.Patch
    return "{0}.{1}.{2}".format(
        (vers >> 24) & 0xFF, (vers >> 8) & 0xFFFF, (vers & 0xFF))

def write(node, progress, data=[]):
    node.sdo['ProgramCommand']['Command'].raw = flash_command.START
    for i, word in enumerate(data):
        node.sdo['ProgramCommand']['Word'].raw = word # Exception on FLASH_FAIL
        if i % 100: # Update 50-step progress bar after every 100 frames
            pct = 100 * i // len(data)
            # I think this is where we will get percent completion
            progressbar(progress,pct)
            print("[{0:50}] ({1:3}%)\r".format(pct // 2 * "=", pct), end="")
        
    # flash_command.END: flashloader generates CRC; SDO exception on AUTH_FAIL
    node.sdo['ProgramCommand']['Command'].raw = flash_command.END
    print("[" + "=" * 50 + "] (100%) \n") # Show finished progress bar

def flash(can_device, can_id, file_name, progress=None):
    if not os.path.isfile(file_name): # Check that the given file exists
        return flash_result.FILE_NOT_FOUND
  
    network = canopen.Network() # Start with an empty network
    system = platform.system()      # Determine the operating system
    print("Connecting to {0} in {1}...".format(can_device, system))
    try: 
        if system == "Windows":
            network.connect(bustype='pcan', 
                            channel='PCAN_USBBUS'+str(int(can_device[-1:])+1), 
                            bitrate=1000000)
        elif system == "Linux":
            network.connect(bustype='socketcan', 
                            channel=can_device, 
                            bitrate=1000000)
    except:
        return flash_result.CANDEV_FAILED
  
    print("Connection succeeded, adding CANopen node id {}...".format(can_id))
    node = network.add_node(can_id, 'flashloader.eds') # Load object dictionary
  
    try:
        node.nmt.state = 'RESET'                       # Reboot into flashloader
        node.nmt.wait_for_heartbeat(timeout=1)         # CANopen boot-up message
        node.sdo["ProgramInfo"]["AutoLaunch"].raw = 0  # Stay in flashloader
    except:
        print("Failed to reset node: {}".format(can_id))
        return flash_result.RESET_FAILED
  
    # Verify flashloader version
    version = get_version(node.sdo['MfgSoftwareVersion'].raw)
    print("Node: {0}, Flashloader version: {1}".format(can_id, version))
    if semver.match(version, '<2.1.0'):
        print("Flashloader version >= 2.1.0 required.")
        return flash_result.VERSION_INCOMPATIBLE
  
    node.sdo.RESPONSE_TIMEOUT = 10 # Increase SDO timeout, flashing takes time
    try:
        node.sdo['ProgramCommand']['Command'].raw = flash_command.ERASE
        print("Flash erase succeeded. Programming...")
    except:
        return flash_result.ERASE_FAILED
  
    with open(file_name, 'rb') as input:
        ext = os.path.splitext(input.name)[-1]             # Get file extension
        if ext == ".ebin":                                 # Is file encrypted?
            for step in ['ProgramAesIv','ProgramAesTag']:  # Write AES IV & tag
                data = array.array("B", input.read(16))    # B -> uint8_t
                print("{0}: {1}".format(step,data))
                for (i, b) in enumerate(data):             # Write each byte
                    node.sdo[step]["{:X}".format(i + 1)].raw = b
            node.sdo['ProgramInfo']["IsEncrypted"].raw = 1 # Enable AES
            write_failure = flash_result.AUTH_FAILED
        elif ext == ".bin":                                # File is unencrypted
            node.sdo['ProgramInfo']["IsEncrypted"].raw = 0 # Disable AES
            write_failure = flash_result.FLASH_FAILED
        else:                                              # Unknown extension?
            print("Invalid extension {}, expected [.bin|.ebin]".format(ext))
            return flash_result.EXTENSION_INVALID
    
        data = input.read() # Read the raw data bytes from the firmware file

    try: # NOTE: Flashloader is expecting 4-byte writes, not ia32-friendly!
        write(node, progress, array.array("I", data)) # Transfer application data via SDOs
    except:
        return write_failure                # Return code depends on file ext
    
    if ext == ".bin": # Check CRC for unencrypted files
        size = node.sdo['ProgramInfo']['MaxSize'].raw # CRC of whole app area
        crc = binascii.crc32(data + b'\xFF' * (size - len(data))) & 0xFFFFFFFF
        if crc != node.sdo["ProgramInfo"]["CRC32"].raw:
            return flash_result.CRC_MISMATCH
  
    # Launch new firmware
    print("Application validated. Launching...")
    node.sdo['ProgramCommand']['Command'].raw = flash_command.LAUNCH
    network.disconnect()
    return flash_result.SUCCESS

def start(can_device, can_id, firmfile, progress=None):

    global node
    # global errors

    # errors = 0

    print("can_device={0}".format(can_device))
    print("can_id={0}".format(can_id))
    # print("edsfile={0}".format(edsfile))
    print("firmware_file={0}".format(firmfile))

    # # Open the CAN device
    # print("Establishing a new network...")
    # network = canopen.Network()

    # time.sleep(0.2) # Wait for any bus-off to clear

    # if platform.system() == "Windows":
    #   network.connect(bustype='pcan', channel='PCAN_USBBUS'+str(int(can_device[-1:])+1), bitrate=1000000)
    # elif platform.system() == "Linux":
    #   network.connect(bustype='socketcan', channel=can_device, bitrate=1000000)

    # print("Connection succeeded, adding CANopen node...")
    # # Add our canopen node along with its object dictionary (for parsing)
    # node = network.add_node(can_id, edsfile)

      # canopen_runner(csvfile, replace_id, start_id, edsfile, v, force, no_warnings):

    # with open(csvfile,'r') as file:
    #     reader = csv.reader(file)
    #     rowcount = len(list(reader)) - 1

    # myfile = open(csvfile, 'r')
    # canopen_runner(myfile, can_id, can_id, None, False, False, False, progress, rowcount)

    # THIS IS WHERE WE ARE RUNNING IT FROM
    result = flash(can_device, can_id, firmfile, progress)
    # print(result)
    # print("Number of errors: {}".format(errors))
    # network.disconnect()
    progressbar(progress, 100)
    # progressbar(progress, "Done")
    # need to print a final error count, after "Done" to catch any errors still!!
    # if errors == 0:
    # #   return True
    #     progressbar(progress, "Pass")
    # else:
    # #   return False 
    #     progressbar(progress, "Fail")
    # return
    if result:
        progressbar(progress, "Fail")
        print("\nFlash failed: " + flash_result.get_string[result])
    else:
        progressbar(progress, "Pass")
        print("Flash succeeded!")

if __name__ == "__main__":
    # Read the command-line arguments
    can_device = sys.argv[1]  # Typically 'can0' for both Linux & Windows
    can_id = int(sys.argv[2]) # 1-127
    file_name = sys.argv[3]   # .bin|.ebin file

    # Flash the new firmware
    # timeStart = time.time()
    result = flash(can_device, can_id, file_name)
    # timeFinish = round(time.time() - timeStart,2)
    # print('Time elapsed: {}'.format(timeFinish))

    if result:
        print("\nFlash failed: " + flash_result.get_string[result])
    else:
        print("Flash succeeded!")
