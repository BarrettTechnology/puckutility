# Map CAN Node IDs (1-16) to their specific joint, motor, and PD configurations.
# Available joints: blj1 blj2 blj3 brj1 brj2 brj3 frj1 frj2 frj3 flj1 flj2 flj3
NODE_CONFIG = {
    3: {
        "joint_name": "flj1",
        "invert_position": False,
        "tk_per_rev": 4096 * ((1 + 58/14) ** 2) ,  # 26:1 gearbox
        "Position_Kp": 30,
        "Velocity_Kp": 0.0025,
        "Velocity_Ki": 0.0,
        "Velocity_Filter": 50,
    },
    13: {
        "joint_name": "flj2",
        "invert_position": False,
        "tk_per_rev": 4096 * ((1 + 58/14) ** 2) ,  # 26:1 gearbox
        "Position_Kp": 30,
        "Velocity_Kp": 0.0025,
        "Velocity_Ki": 0.0,
        "Velocity_Filter": 50,
    },
    14: {
        "joint_name": "flj3",
        "invert_position": False,
        "tk_per_rev": 4096 * ((1 + 58/14) ** 2) ,  # 26:1 gearbox
        "Position_Kp": 30,
        "Velocity_Kp": 0.0025,
        "Velocity_Ki": 0.0,
        "Velocity_Filter": 50,
    },
    # Add other nodes here up to ID 16
}
