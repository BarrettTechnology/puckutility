# Dead-Time Compensation — Implementation Plan

## Background

PWM inverter dead-time creates a systematic phase voltage error whose sign depends
on the direction of phase current. Even after minimizing dead-time duration via
itiming calibration, the remaining interval still produces a predictable distortion
that is worst at low speed and low torque — exactly the operating regime that
matters most for haptic transparency.

Current sensor bias (ibias) and encoder compensation are already handled. Dead-time
feedforward is the next highest-value compensation for force quality.

---

## Firmware (pwm.c)

Insert after the current PI computes Vd/Vq, before inverse Park and SVPWM.

### 1. Voltage error magnitude

```c
float V_err = (T_dead / T_period) * V_bus;
```

T_dead and T_period are known from the PWM configuration. V_bus comes from an
existing ADC channel or fixed supply constant.

### 2. Reconstruct phase currents from α/β

```c
float I_a =  I_alpha;
float I_b = -I_alpha * 0.5f + SQRT3_2 * I_beta;
float I_c = -I_alpha * 0.5f - SQRT3_2 * I_beta;
```

### 3. Per-phase correction with zero-crossing smoothing

Hard sign() causes chattering when current reverses. Use a smooth approximation:

```c
float sign_smooth(float I, float thresh) {
    return I / (fabsf(I) + thresh);
}

float V_a_comp = V_err * sign_smooth(I_a, I_THRESH);
float V_b_comp = V_err * sign_smooth(I_b, I_THRESH);
float V_c_comp = V_err * sign_smooth(I_c, I_THRESH);
```

I_THRESH is a tunable threshold (units: ADC counts or amps depending on where
in the pipeline this sits). Sets the width of the zero-crossing dead-band.

### 4. Transform correction into d-q frame

Apply Clarke (phase → α/β) then Park (α/β → d/q) to the compensation voltages
and add ΔVd, ΔVq to the PI output before SVPWM.

---

## Calibration (puckutility)

No new calibration routine is likely needed — T_dead and V_bus are already known.
Optionally, an empirical gain-tuning step could be added:

- Command a low-amplitude sinusoidal Iq
- Measure 6th harmonic content in d-q frame currents (where dead-time distortion
  concentrates in FOC)
- Scale the compensation gain to minimise it

---

## Design Decisions

- **I_THRESH** — the main tunable parameter. Too small: chattering at zero-crossing.
  Too large: under-compensation at low current. Empirically tune per motor/drive.
- **V_bus source** — fixed constant vs live ADC reading. Live reading is more
  accurate over supply variation; constant is simpler.
- **Gain scaling** — start at theoretical value (T_dead/T_period × V_bus), tune
  empirically if residual 6th harmonic is measurable.

---

## Voltage Error Magnitude — 80 kHz PWM

T_period = 1 / 80 000 Hz = 12.5 µs

| Dead-time | V_bus | V_err  | % of V_bus |
|-----------|-------|--------|------------|
|   250 ns  |  24 V | 0.48 V |    2.0 %   |
|   250 ns  |  48 V | 0.96 V |    2.0 %   |
|  1000 ns  |  24 V | 1.92 V |    8.0 %   |
|  1000 ns  |  48 V | 3.84 V |    8.0 %   |
|  1600 ns  |  24 V | 3.07 V |   12.8 %   |
|  1600 ns  |  48 V | 6.14 V |   12.8 %   |

V_err is the worst-case per-phase voltage error magnitude. At 250 ns (post-itiming)
and 48 V the error is ~1 V — meaningful at low Iq commands. At 1000–1600 ns it
becomes a dominant disturbance regardless of bus voltage.
