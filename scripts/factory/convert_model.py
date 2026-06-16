#!/usr/bin/env python3
"""Convert a Puck's model identity and persist it to the Puck.

The Puck model is encoded in the CANopen Identity Object **Product Code**
(``0x1018`` sub-2), which the firmware exposes as read/write. This tool:

  1. reads the Puck's current product code (so you can see what it is now),
  2. writes the product code for the model you select with a flag,
  3. saves the object dictionary to EEPROM (Save All), so the change persists,
  4. reboots the Puck, and
  5. verifies the new model on a fresh connection.

It changes ONLY the model identity -- it does NOT load motor parameters, gains,
or current limits. After converting, upload the matching config CSV (Puck
Utility -> File, or the headless ``--config`` path) if you also need the model's
parameter set.

Usage::

    convert_model.py --42                 # convert active puck (node 127) to P4-42
    convert_model.py --model 16 --id 5    # convert node 5 to P4-16
    convert_model.py --37 --can can1 -y   # node 127 on can1, no confirmation

Exit status is 0 on a verified conversion, non-zero on any failure.
"""
import argparse
import os
import sys
import time

# --- Make the repo-root modules / EDS importable regardless of cwd -----------
# This script lives at <repo>/scripts/convert/, so the repo root is three levels
# up. Import can_backend (the shared CAN adapter factory) and resolve puck4.eds
# from there so the tool works no matter where it is invoked from.
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

import canopen          # noqa: E402  (after sys.path tweak)
import can_backend      # noqa: E402

EDS_PATH = os.path.join(REPO_ROOT, 'puck4.eds')

PRODUCT_CODE_INDEX = 0x1018
PRODUCT_CODE_SUB   = 2
SAVE_KEY           = 0x65766173  # 'SAVE' little-endian -> Save All to EEPROM

# Model number -> product code to WRITE when converting to that model.
MODEL_TO_CODE = {16: 5707, 32: 5760, 37: 1323, 42: 5755}

# Product code -> model name for READBACK / display. P4-37 ships under two
# codes (1323 and the older 1950), so both map back to 'P4-37'.
CODE_TO_MODEL = {
    5707: 'P4-16',
    5760: 'P4-32',
    1323: 'P4-37',
    1950: 'P4-37',
    5755: 'P4-42',
}


def model_name(code):
    """Human model name for a product code, or 'unknown (<code>)'."""
    return CODE_TO_MODEL.get(code, 'unknown ({})'.format(code))


def read_product_code(node):
    return int(node.sdo[PRODUCT_CODE_INDEX][PRODUCT_CODE_SUB].raw)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog='convert_model.py',
        description='Convert a Puck model: write & save CANopen Product Code '
                    '(0x1018:2) and reboot.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Examples:\n'
               '  convert_model.py --42\n'
               '  convert_model.py --model 16 --id 5\n'
               '  convert_model.py --37 --can can1 -y')
    target = p.add_mutually_exclusive_group(required=True)
    for m in sorted(MODEL_TO_CODE):
        target.add_argument('--{}'.format(m), dest='model', action='store_const',
                            const=m, help='convert the Puck to P4-{}'.format(m))
    target.add_argument('--model', dest='model', type=int,
                        choices=sorted(MODEL_TO_CODE),
                        help='convert the Puck to P4-<MODEL> (16/32/37/42)')
    p.add_argument('--can', default='can0',
                   help='CAN interface (default: can0)')
    p.add_argument('--id', type=int, default=127,
                   help='Puck node id (default: 127)')
    p.add_argument('-y', '--yes', action='store_true',
                   help='skip the confirmation prompt')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    target_code = MODEL_TO_CODE[args.model]
    target_name = 'P4-{}'.format(args.model)

    if not os.path.isfile(EDS_PATH):
        print('ERROR: object dictionary not found at {}'.format(EDS_PATH))
        return 1

    # --- Connect and read the current model ---------------------------------
    print('Connecting to {} (node {})...'.format(args.can, args.id))
    try:
        network = can_backend.make_network(args.can, bitrate=1_000_000)
    except Exception as e:
        print('ERROR: could not open {}: {}'.format(args.can, e))
        return 1
    node = network.add_node(args.id, EDS_PATH)

    try:
        current = read_product_code(node)
    except Exception as e:
        print('ERROR: could not read product code from node {} -- is the Puck '
              'powered and on the bus?\n       {}'.format(args.id, e))
        network.disconnect()
        return 1

    print('  Current model: {} (product code {})'.format(model_name(current), current))
    print('  Target  model: {} (product code {})'.format(target_name, target_code))

    if current == target_code:
        print('Puck is already {} -- nothing to do.'.format(target_name))
        network.disconnect()
        return 0

    # --- Confirm ------------------------------------------------------------
    if not args.yes:
        try:
            resp = input('Convert node {} from {} to {}? [y/N] '.format(
                args.id, model_name(current), target_name)).strip().lower()
        except EOFError:
            resp = ''
        if resp not in ('y', 'yes'):
            print('Aborted.')
            network.disconnect()
            return 1

    # --- Write the new product code -----------------------------------------
    try:
        node.sdo[PRODUCT_CODE_INDEX][PRODUCT_CODE_SUB].raw = target_code
        print('Wrote product code {} ({}).'.format(target_code, target_name))
    except Exception as e:
        print('ERROR: failed to write product code: {}'.format(e))
        network.disconnect()
        return 1

    # --- Save to EEPROM (Save All) ------------------------------------------
    # The save takes ~0.55 s and the flashloader/app may not answer the SDO
    # immediately, so bump the response timeout the way the app's config-save
    # does, then restore it.
    print('Saving to EEPROM...')
    default_timeout = canopen.sdo.SdoClient.RESPONSE_TIMEOUT
    canopen.sdo.SdoClient.RESPONSE_TIMEOUT = 1.0
    try:
        node.sdo['Save']['All'].raw = SAVE_KEY
    except Exception as e:
        print('ERROR: save to EEPROM failed: {}'.format(e))
        canopen.sdo.SdoClient.RESPONSE_TIMEOUT = default_timeout
        network.disconnect()
        return 1
    finally:
        canopen.sdo.SdoClient.RESPONSE_TIMEOUT = default_timeout

    # --- Reboot so the Puck comes up as the new model -----------------------
    print('Rebooting puck...')
    network.send_message(0x0, [0x81, args.id])  # NMT reset node
    time.sleep(0.5)
    network.disconnect()

    # --- Verify on a fresh connection (the node just rebooted) --------------
    print('Verifying...')
    time.sleep(0.5)
    try:
        vnet = can_backend.make_network(args.can, bitrate=1_000_000)
        vnode = vnet.add_node(args.id, EDS_PATH)
        new_code = read_product_code(vnode)
        vnet.disconnect()
    except Exception as e:
        print('WARNING: wrote & saved {} but could not verify after reboot: {}'
              .format(target_name, e))
        return 1

    if new_code == target_code:
        print('SUCCESS: puck is now {} (product code {}).'.format(target_name, new_code))
        print('Note: this changed the model IDENTITY only. Upload the {} config '
              'CSV if you also need its motor parameters.'.format(target_name))
        return 0

    print('ERROR: verification failed -- puck reports {} (product code {}), '
          'expected {} ({}).'.format(
              model_name(new_code), new_code, target_name, target_code))
    return 1


if __name__ == '__main__':
    sys.exit(main())
