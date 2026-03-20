import json
import math
import struct

# Get joint positions from a json file


def get_joint_positions(file_path, joint_name):
    with open(file_path, 'r') as file:
        data = json.load(file)
    result = {entry['time']: entry['joint_positions'][joint_name] for entry in data}
    return result


# Remap a value from one range to another
def remap(input, inMin, inMax, outMin, outMax):
    return (input - inMin) * (outMax - outMin) / (inMax - inMin) + outMin


def rad2counts(rad, counts_per_rev):
    return rad * counts_per_rev / (2 * math.pi)


def counts2rad(counts, counts_per_rev):
    return counts * (2 * math.pi) / counts_per_rev


def deg2counts(deg, counts_per_rev):
    return deg * counts_per_rev / 360.0


def counts2deg(counts, counts_per_rev):
    return counts * 360.0 / counts_per_rev


def rpm2rad(rpm: float):
    return rpm * math.pi / 30


def rad2rpm(rad: float):
    return rad * 60 / (2*math.pi)


def deg2rad(deg: float) -> float:
    return deg * math.pi / 180


def rad2deg(rad: float):
    return rad * 180 / math.pi


def rpm2counts(rpm: float, counts_per_rev=4096):
    return rpm * counts_per_rev / 60.0


def limit_value(input, min_limit, max_limit):
    lower = min(min_limit, max_limit)
    upper = max(min_limit, max_limit)
    return max(min(input, upper), lower)


class IIRFilter:
    def __init__(self, alpha=0.1):
        self.alpha = alpha
        self.filtered_value = None

    def update(self, new_value):
        if self.filtered_value is None:
            self.filtered_value = new_value
        else:
            self.filtered_value = self.alpha * new_value + (1 - self.alpha) * self.filtered_value
        return self.filtered_value


def float_to_ieee_decimal(float_val):
    """
    Convert a Python float to its IEEE 754 representation as a decimal integer.

    Args:
        float_val (float): The floating point value to convert

    Returns:
        int: Decimal integer representation of the IEEE 754 encoding
    """
    # Pack float to bytes using IEEE 754 format
    packed_bytes = struct.pack('<f', float_val)

    # Convert packed bytes to an integer
    ieee_decimal = int.from_bytes(packed_bytes, byteorder='little')

    return ieee_decimal
