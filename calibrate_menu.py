# calibrate.py
import wx
import canopen
import time
import math
import webbrowser
import configparser
import platform

def _strip_ini_quotes(value):
    if value and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value if value else value

# TODO - No active issues

class calibrate():
    def calibrate_all_pucks(self, event):
        print(self.network.scanner.nodes)
        starting_id = self.getID()
        if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            # print("No active puck")
            return False
        for i in self.network.scanner.nodes:
            print(i)
            indexID = self.network.scanner.nodes.index(i)
            self.choice_id.SetSelection(indexID) # Move to next ID for calibration
            self.select_id(None)

            # print("Running full calibration for Puck {}".format(self.getID()))
            self.calibrate_all(None)
            # self.calibrate_ibias(None)
            # self.calibrate_igainfactor(None)
            # self.calibrate_enczero(None)

        indexID = self.network.scanner.nodes.index(starting_id)
        self.choice_id.SetSelection(indexID) # Return to starting ID after completion
        self.select_id(None)

    def calibrate_all(self, event):  # wxGlade: wxp3_frame.<event_handler>
        # Try to add calibrate all step!
        if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            # print("No active puck")
            return False
        print("Running full calibration for Puck {}".format(self.getID()))
        continueCal = self.test_encoder(None, True)
        self.Disable()
        if continueCal == False:
          print('Ending calibration...')
          self.Enable()
          return
        continueCal = self.calibrate_ibias(None, True)
        if continueCal == False:
          print('Ending calibration...')
          self.Enable()
          return
        continueCal = self.calibrate_igainfactor(None, True)
        if continueCal == False:
          print('Ending calibration...')
          self.Enable()
          return
        self.calibrate_enczero(None, True)
        if continueCal == False:
          print('Ending calibration...')
          self.Enable()
          return
        
        self.requireCal = False
        self.Enable()
        #event.Skip()

    def calibrate_ibias(self, event, calAll=False):  # wxGlade: wxp3_frame.<event_handler>
        # print("Event handler 'calibrate_ibias'")
        if calAll==False:
          if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            return False
          self.Disable()
        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0 # Reset lastMode
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE
        
        if self.ADC_ON == True:
           self.on_off_adc(self)
           self.adcWasON = True
        else:
           self.adcWasON = False
        self.frame_statusbar.SetStatusText("Calibrating ibias...", 1)
        self.frame_statusbar.Update()
        wx.Yield()


        # Clear faults, RTSO, OpEnabled
        print("Going OpEnabled")
        self.node.sdo["ControlWord"].raw = 0x80
        self.node.sdo["ControlWord"].raw = 0x06
        self.node.sdo["ControlWord"].raw = 0x0F

        self.node.sdo['Theta_e'].raw = 0x7FFF # Stall @ Alpha Peak (+pi)

        self.node.sdo['Motor']['ud'].raw = 000

        # Set Mode to Voltage
        print("Setting Mode = VOLTAGE MODE")
        self.node.sdo["SetModeOfOperation"].raw = 12
        time.sleep(1) # Wait at least 75 ms for the filters to settle (2 seconds seems to be the sweet spot)

        # Calibrate iSense
        for channel in ['Alpha', 'Beta']:
          print("Previous {0} iSense bias = {1}".format(channel, self.node.sdo[channel]['Bias'].raw))
          filt = self.node.sdo[channel]['Filtered'].raw # Q12.4
          filt = (filt >> 4) + ((filt & 0x0008) >> 3) # Round Q12.4 to Q12.0
          self.node.sdo[channel]['Bias'].raw = filt
          print("New {0} iSense bias = {1}".format(channel, filt))

        self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x03) # Save Alpha iSense cal to EE
        self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x03) # Save Beta iSense cal to EE

        # Check Bounds for error!!
        error = 0.5 # 5% error # .03 # 3% error

        a_bias = self.node.sdo['Alpha']['Bias'].raw
        b_bias = self.node.sdo['Beta']['Bias'].raw

        # Set Mode to Idle (0)
        print("Setting Mode = IDLE")
        self.node.sdo["SetModeOfOperation"].raw = 0
        # time.sleep(1) # Wait at least 75 ms for the filters to settle

        if a_bias > 2048 * (1 + error) or a_bias < 2048 * (1 - error) or b_bias > 2048 * (1 + error) or b_bias < 2048 * (1 - error) :
          print('iSense Bias out of bounds!')
          msg = "iSense Bias out of bounds!" \
          "\n\nAlpha Bias: {}" \
          "\nBeta Bias: {}" \
          "\nAcceptable Range: {} - {}" \
          "\n\nDebugging steps:" \
          "\n- Ensure proper configuration file has been loaded" \
          "\n- Verify phase leads are properly connected" \
          "\n\nWould you like to continue calibration?"  .format(a_bias,b_bias,round(2048*(1-error)),round(2048*(1+error)))
          dlg = wx.MessageDialog(None,msg,'Warning!',wx.YES_NO | wx.ICON_WARNING)
          answer = dlg.ShowModal()
          dlg.Destroy()
          print("Encoder readings unstable...")

        self.frame_statusbar.SetStatusText("Ready", 1)
        #self.text_ctrl_6.ChangeValue(str(self.node.sdo['Cal']['iSense1'].raw))
        if self.ADC_ON == False and self.adcWasON == True:
           self.on_off_adc(self)
        if calAll==False:
          self.Enable()
        try:
          if answer == wx.ID_YES:
              return True
          if answer == wx.ID_NO:
              return False
        except:
          pass

    def calibrate_igainfactor(self, event, calAll=False):  # wxGlade: wxp3_frame.<event_handler>
        # print("Event handler 'calibrate_igainfactor'")
        if calAll==False:
          if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            return False
          self.Disable() 
        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0 # Reset lastMode
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE

        if self.ADC_ON == True:
           self.on_off_adc(self)
           self.adcWasON = True
        else:
           self.adcWasON = False

        self.frame_statusbar.SetStatusText("Calibrating igainfactor...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        # Set Alpha & Beta gainfactors to 1.0 in Q4.12
        self.node.sdo['Alpha']['Gainfactor'].raw = 4096 
        self.node.sdo['Beta']['Gainfactor'].raw = 4096 

        # Clear faults, RTSO, OpEnabled
        print("Going OpEnabled")
        self.node.sdo["ControlWord"].raw = 0x80
        self.node.sdo["ControlWord"].raw = 0x06
        self.node.sdo["ControlWord"].raw = 0x0F

        # Set Mode to PhaseVoltageAngle (12)
        print("Setting Mode = VOLTAGE")
        self.node.sdo["SetModeOfOperation"].raw = 12

        # Write theta_e, ud, StatsMode, vel
        # theta_e is 16-bit signed from -pi to +pi
        self.node.sdo['Theta_e'].raw = 0x7FFF # Stall @ Alpha Peak (+pi)

        # Read this motor's calibration current (mA)
        calibration_current = self.node.sdo['Calibration']['i_cal'].raw

        # Read the motor.peak (mA)
        i_peak = self.node.sdo['Calibration']['i_peak'].raw

        # If calibration current is greater than i_peak, limit
        if calibration_current > i_peak:
           calibration_current = i_peak

        time.sleep(1) # Wait at least 75 ms for the filters to settle

        # Increase Motor d-axis voltage (/1000 of i_peak)
        # until measured d-axis current > calibration_current mA or ud > 32000
        motor_ud = 0
        motor_id = self.node.sdo['Motor']['id'].raw
        while (motor_id < 1000 and self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak) < calibration_current and motor_ud < 32000:
          print("alpha = {0}, beta = {1}, id = {2}, iq = {3}, ud = {4}".format(
            self.node.sdo['Alpha']['Raw'].raw, 
            self.node.sdo['Beta']['Raw'].raw, 
            round(self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak, 2), 
            self.node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak,
            self.node.sdo['Motor']['ud'].raw))
          motor_ud += 100 # 25 # was 100, then 50
          self.node.sdo['Motor']['ud'].raw = motor_ud
          time.sleep(0.05)

        time.sleep(1) # Wait at least 75 ms for the filters to settle

        a_filt = self.node.sdo['Alpha']['Filtered'].raw # Q12.4
        a_filt = (a_filt >> 4) + ((a_filt & 0x0008) >> 3) # Round Q12.4 to Q12.0
        print("Peak Alpha = {0} at motor current = {1} mA (theta_e = {2:0.2f})".format(
          a_filt, 
          round(self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak, 2), 
          self.node.sdo['Theta_e'].raw / 32768.0 * 3.14159))

        self.node.sdo['Theta_e'].raw = -0x4000 # Stall @ Beta Peak (-pi/2)
        time.sleep(1) # Wait at least 75 ms for the filters to settle

        b_filt = self.node.sdo['Beta']['Filtered'].raw # Q12.4
        b_filt = (b_filt >> 4) + ((b_filt & 0x0008) >> 3) # Round Q12.4 to Q12.0
        print("Peak Beta = {0} at motor current = {1} mA (theta_e = {2:0.2f})".format(
          b_filt, 
          self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak, 
          self.node.sdo['Theta_e'].raw / 32768.0 * 3.14159))

        self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE

        abias = self.node.sdo['Alpha']['Bias'].raw
        bbias = self.node.sdo['Beta']['Bias'].raw

        # Scale b by (a-abias)/(b-bbias) to match a's amplitude while accounting for bias
        gainfactor = self.node.sdo['Beta']['Gainfactor'].raw = 4096 * (a_filt - abias) / (b_filt - bbias) # Gain in Q4.12
        gainfactor = round(gainfactor)
        print("New Beta Gainfactor = {0}".format(self.node.sdo['Beta']['Gainfactor'].raw))

        # Check Bounds for error!! Can increase to 10% if needed
        error = 0.10 # 10%

        if gainfactor > round(4096 * (1 + error)) or gainfactor < round(4096 * (1 - error)):
          print('Beta Gainfactor out of bounds!')
          # Bad Encoder reading (error dialog! debug steps)
          # Offer to continue or cancel calibration?
          msg = "Beta Gainfactor out of bounds! \n\nGainfactor: {}" \
          "\nAcceptable Range: {} - {}" \
          "\n\nDebugging steps:" \
          "\n- Ensure proper configuration file has been loaded" \
          "\n- Verify phase leads are properly connected" \
          "\n\nWould you like to continue calibration?"  .format(gainfactor,round(4096*(1-error)),round(4096*(1+error)))
          dlg = wx.MessageDialog(None,msg,'Warning!',wx.YES_NO | wx.ICON_WARNING)
          answer = dlg.ShowModal()
          dlg.Destroy()

        self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x06) # Save Alpha gainfactor to EE
        self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x06) # Save Beta gainfactor to EE

        self.frame_statusbar.SetStatusText("Ready", 1)

        if self.ADC_ON == False and self.adcWasON == True:
           self.on_off_adc(self)

        if calAll==False:
          self.Enable()
        try:
          if answer == wx.ID_YES:
              return True
          if answer == wx.ID_NO:
              return False
        except:
          pass

    def calibrate_itiming(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'calibrate_itiming' not implemented!")
        # Tune the current sampling moment to minimize noise
        # Collect noise statistics at/near falling edge of the widest PWM, in all 6 sectors

        event.Skip()

    def calibrate_islope(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'calibrate_islope' not implemented!")
        event.Skip()

    def calibrate_enczero(self, event, calAll=False):  # wxGlade: wxp3_frame.<event_handler>
        # print("Event handler 'calibrate_enczero'")
        if calAll==False:
          if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            return False
          self.Disable()
        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0 # Reset lastMode
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE
        
        if self.ADC_ON == True:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        self.frame_statusbar.SetStatusText("Calibrating encoder...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        # Clear faults, RTSO, OpEnabled
        print("Going OpEnabled")
        self.node.sdo["ControlWord"].raw = 0x80
        self.node.sdo["ControlWord"].raw = 0x06
        self.node.sdo["ControlWord"].raw = 0x0F
        
        # Set Mode to PhaseVoltageAngle (12)
        print("Setting Mode = VOLTAGE")
        self.node.sdo["SetModeOfOperation"].raw = 12

        # Write theta_e, ud, StatsMode, vel
        # theta_e is 16-bit signed from -pi to +pi
        #self.node.sdo['Calibration']['e_polarity'].raw = 1
        self.node.sdo['Theta_e'].raw = -0x1000 # -pi/2

        # Read this motor's calibration current (mA)
        calibration_current = self.node.sdo['Calibration']['i_cal'].raw

        # Read the motor.peak (mA)
        i_peak = self.node.sdo['Calibration']['i_peak'].raw

        # If calibration current is greater than i_peak, limit
        if calibration_current > i_peak:
           calibration_current = i_peak

        # Increase Motor d-axis voltage until measured d-axis current > calibration_current mA or ud > 32000
        motor_ud = 0
        motor_id = self.node.sdo['Motor']['id'].raw
        while (motor_id < 1000 and self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak) < calibration_current and motor_ud < 32000:
          print("id = {0}, ud = {1}".format(
            self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak, 
            self.node.sdo['Motor']['ud'].raw))
          motor_ud += 100
          self.node.sdo['Motor']['ud'].raw = motor_ud
          time.sleep(0.05)

        # Drive from theta_e = -90 to 0 in 10 steps of 0.05s
        # Capture RawPosition when commanding theta_e = 0
        # Also determine e_polarity by watching the raw encoder direction
        pos0 = self.node.sdo['Encoder']['RawPosition'].raw
        startPos1 = self.node.sdo['PositionFeedback'].raw
        for i in range(int(-0x1000), 0, int(0x1000/32)):
          self.node.sdo['Theta_e'].raw = i
          time.sleep(0.05)
        time.sleep(0.25)
        pos1 = self.node.sdo['Encoder']['RawPosition'].raw
        print("After approaching theta_e = 0 from -22.5, Encoder raw = {0}".format(pos1))

        zeroPos1 = self.node.sdo['PositionFeedback'].raw

        # Drive from theta_e = +90 to 0 in 10 steps of 0.05s
        # Capture RawPosition when commanding theta_e = 0
        self.node.sdo['Theta_e'].raw = 0x1000
        time.sleep(1)
        startPos2 = self.node.sdo['PositionFeedback'].raw
        for i in range(int(0x1000), 0, int(-0x1000/32)):
          self.node.sdo['Theta_e'].raw = i
          time.sleep(0.05)
        time.sleep(0.25)
        pos2 = self.node.sdo['Encoder']['RawPosition'].raw
        print("After approaching theta_e = 0 from +22.5, Encoder raw = {0}".format(pos2))
        zeroPos2 = self.node.sdo['PositionFeedback'].raw

        # Take the average of the two measurements, store e_zero
        encoder_resolution = self.node.sdo['EncoderConfig']['Resolution'].raw
        motor_poles = self.node.sdo['Calibration']['poles'].raw
        cts_per_elec_cyc = encoder_resolution * 2 / motor_poles
        if abs(pos1-pos2) >  encoder_resolution / 2:
          if pos1 > pos2:
            pos1 += encoder_resolution
          else:
            pos2 += encoder_resolution
        pos = (pos1 + pos2) / 2
        pos = pos % cts_per_elec_cyc
        pos = int(pos)

        # Calculate e_polarity
        if abs(pos1-pos0) < (cts_per_elec_cyc / 2): 
          # If there was no rollover during the initial -90..0 movement
          self.node.sdo['Calibration']['e_polarity'].raw = math.copysign(1, pos1-pos0)
        else: 
          # We rolled over
          self.node.sdo['Calibration']['e_polarity'].raw = -math.copysign(1, pos1-pos0)
        self.node.sdo['Save']['Single'].raw = ((0x3011 << 8) | 0x02) # Save e_polarity to EE
        print("Electrical polarity = {0}".format(self.node.sdo['Calibration']['e_polarity'].raw))

        previous_zero = self.node.sdo['Calibration']['e_zero'].raw

        print("Previous electrical zero = {0}".format(previous_zero))
        self.node.sdo['Calibration']['e_zero'].raw = pos
        self.node.sdo['Save']['Single'].raw = ((0x3011 << 8) | 0x01) # Save e_zero to EE
        print("New electrical zero = {0}".format(pos))

        pos_change1 = round(abs(startPos1 - zeroPos1) * (360/4096) * motor_poles)
        pos_change2 = round(abs(startPos2 - zeroPos2) * (360/4096) * motor_poles)

        # Check Bounds for error!!
        error = .25 # 25%
        expected_change = 22.5

        self.node.sdo["SetModeOfOperation"].raw = 0 # IDLE

        if pos_change1 < round(expected_change * (1 - error)) or pos_change2 < round(expected_change * (1 - error)):
          print('Encoder Zero Failed!')
          # Bad Encoder reading (error dialog! debug steps)
          # Can grab kt and cal current to determine required torque
          cal_torque = calibration_current * self.node.sdo['Calibration']['kt'].raw / 1000
          msg = "Encoder Zero Failed! \n\nFirst Jump: {}°" \
          "\nSecond Jump: {}°" \
          "\nExpected Jump: >= {}°" \
          "\n\nDebugging steps:" \
          "\n- Ensure proper configuration file has been loaded" \
          "\n- Verify output friction is less than cal torque for the motor ({}mNm)" \
          "\n\nWould you like to continue calibration?"  .format(pos_change1,pos_change2,round(22.5*(1-error)),cal_torque)
          dlg = wx.MessageDialog(None,msg,'Warning!',wx.YES_NO | wx.ICON_WARNING)
          answer = dlg.ShowModal()
          dlg.Destroy()

        self.frame_statusbar.SetStatusText("Ready", 1)

        if self.ADC_ON == False and self.adcWasON == True:
            self.on_off_adc(self)

        if calAll==False:
          self.Enable()
        try:
          if answer == wx.ID_YES:
              return True
          if answer == wx.ID_NO:
              return False
        except:
          pass

    def calibrate_encdir(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'calibrate_encdir' not implemented!")
        event.Skip()

    def calibrate_enclag(self, event,calAll=False):  # wxGlade: wxp3_frame.<event_handler>
        # print("Event handler 'calibrate_enclag'")

        if self.ADC_ON == True:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        if calAll==False:
          if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            return False
          self.Disable()

        self.frame_statusbar.SetStatusText("Calibrating Encoder Lag...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        # Set Mode to Idle (0)
        print("Setting Mode = IDLE")
        self.node.sdo["SetModeOfOperation"].raw = 0
        time.sleep(1) # Wait at least 75 ms for the filters to settle

        # Clear faults, RTSO, OpEnabled
        print("Going OpEnabled")
        self.node.sdo["ControlWord"].raw = 0x80
        self.node.sdo["ControlWord"].raw = 0x06
        self.node.sdo["ControlWord"].raw = 0x0F
      
        # Set Mode to Torque (4)
        print("Setting Mode = TORQUE")
        self.node.sdo["SetModeOfOperation"].raw = 4
        self.node.sdo['EncoderConfig']['LagFactor'].raw = 0

        # Increase TargetTorque until iq.fbk = 1000 mA
        cmd_value = 0
        self.node.sdo["TargetTorque"].raw = cmd_value # Send
        # q_fbk = 0
        while True:
         self.node.sdo["TargetTorque"].raw = cmd_value # Send
         time.sleep(0.05)
         q_fbk = self.node.sdo['CurrentFeedback'].raw
         print("TargetTorque = {0}, CurrentFeedback = {1} mA".format(cmd_value, q_fbk))
         if q_fbk > 1000 or cmd_value == 1000:
           break
         cmd_value += 50
          
        # Set the number of lag increments to attempt without setting a new max_vel
        max_cycles = 50

        # Init: cycles = 0, max = 0, lag = 0
        cycles = 0
        max_vel = 0
        lag = 0

        while True:
          # Read vel.fbk
          vel = abs(self.node.sdo["VelocityFeedback"].raw)
          # If |vel.fbk| > max, update max, remember lag, reset cycles to zero
          if vel > max_vel:
            max_vel = vel
            saved_lag_1 = lag
            cycles = 0
          else: # Else, ++cycles
            cycles += 1
          
          print("Lag: {0}, Vel: {1}, MaxVel: {2}, Cycles: {3}".format(lag, vel, max_vel, cycles))
          # Increase EncoderLag until cycles == max_cycles
          lag += 1
          self.node.sdo['EncoderConfig']['LagFactor'].raw = lag
          if cycles > max_cycles:
            break
          time.sleep(0.05)

        # Invert TargetTorque
        self.node.sdo["TargetTorque"].raw = -cmd_value # Send
        self.node.sdo['EncoderConfig']['LagFactor'].raw = 0
        
        time.sleep(0.5)

        # Init: cycles = 0, max = 0, lag = 0
        cycles = 0
        max_vel = 0
        lag = 0

        while True:
          # Read vel.fbk
          vel = abs(self.node.sdo["VelocityFeedback"].raw)
          # If |vel.fbk| > max, update max, remember lag, reset cycles to zero
          if vel > max_vel:
            max_vel = vel
            saved_lag_2 = lag
            cycles = 0
          else: # Else, ++cycles
            cycles += 1
          
          print("Lag: {0}, Vel: {1}, MaxVel: {2}, Cycles: {3}".format(lag, vel, max_vel, cycles))
          # Increase EncoderLag until cycles == max_cycles
          lag += 1
          self.node.sdo['EncoderConfig']['LagFactor'].raw = lag
          if cycles > max_cycles:
            break
          time.sleep(0.05)

        # Take the average of the two lags
        lag = (saved_lag_1 + saved_lag_2) / 2
        print("Lag_1: {0}, Lag_2: {1}, Setting LagFactor: {2}".format(saved_lag_1, saved_lag_2, lag))

        # Store the LagFactor
        self.node.sdo['EncoderConfig']['LagFactor'].raw = lag
        self.node.sdo['Save']['Single'].raw = ((0x3013 << 8) | 0x05) # Save lag to EE

        # Set Mode to Idle (0)
        print("Setting Mode = IDLE")
        self.node.sdo["SetModeOfOperation"].raw = 0

        if calAll==False:
          self.Enable()

        self.frame_statusbar.SetStatusText("Ready", 1)

        if self.ADC_ON == False and self.adcWasON == True:
            self.on_off_adc(self)
        if calAll==False:
          self.Enable()

    def test_encoder(self,event,calAll=False):
        # print("Testing Encoder...")
        if calAll==False:
          if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            # print("No active puck")
            return False
          self.Disable()
        self.frame_statusbar.SetStatusText("Testing Encoder...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        if self.ADC_ON == True:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        # Set Mode to Idle (0)
        print("Setting Mode = IDLE")
        self.node.sdo["SetModeOfOperation"].raw = 0
        time.sleep(1) # Wait at least 75 ms for the filters to settle
        timeEnd = time.time() + 1
        Pos = []
        while time.time() < timeEnd:
          Pos.append(self.node.sdo['PositionFeedback'].raw)
        posDif = max(Pos) - min(Pos)
        print("Max Pos: {} Min Pos: {} Diff: {}".format(max(Pos), min(Pos), posDif))
        maxDif = 8

        if(posDif <= maxDif):
          # Good, passed test
          pass
        if(posDif > maxDif):
          # Bad Encoder reading (error dialog! debug steps)
          # Offer to continue or cancel calibration?
          msg = "Encoder Readings Unstable! \n\nEncoder variation: {} counts" \
          "\nMax Acceptable Variation: {} counts" \
          "\n\nDebugging steps:" \
          "\n- Ensure magnet to encoder spacing is 1.5mm +/- 0.5mm" \
          "\n- Verify magnet concentric to the shaft and rotates properly" \
          "\n\nWould you like to continue calibration?"  .format(posDif,maxDif)
          dlg = wx.MessageDialog(None,msg,'Warning!',wx.YES_NO | wx.ICON_WARNING)
          answer = dlg.ShowModal()
          dlg.Destroy()
          print("Encoder readings unstable...")

        self.frame_statusbar.SetStatusText("Ready", 1)

        if self.ADC_ON == False and self.adcWasON == True:
            self.on_off_adc(self)
        if calAll==False:
          self.Enable()
        try:
          if answer == wx.ID_YES:
              return True
          if answer == wx.ID_NO:
              return False
        except:
          pass

    def set_user_dir(self, event):  # wxGlade: wxp3_frame.<event_handler>
        if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
          return False
        
        print("Event handler 'set_user_dir'")
        
        self.node.sdo['EncoderConfig']['UserPolarity'].raw = 1 # Assume positive to start
        encoder_resolution = self.node.sdo['EncoderConfig']['Resolution'].raw
        starting_position = self.node.sdo['PositionFeedback'].raw

        self.frame_statusbar.SetStatusText("Please turn motor in positive (+) direction...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        done = False
        while not done:
          print("Waiting...")
          time.sleep(1)
          ending_position = self.node.sdo['PositionFeedback'].raw
          if abs(starting_position - ending_position) > (encoder_resolution / 8):
            done = True
          
    def open_support_page(self, event):
        print('Opening support page...')
        webbrowser.open_new(r'PuckUtilityAppGuide.pdf')

    def update_all(self, event):
        print('Updating all Pucks...')
        if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            return False
        print(self.network.scanner.nodes)
        starting_id = self.getID()

        # File browser
        if platform.system() == "Windows":
            directory = '../firmware'
        else:
            directory = 'firmware/'

        # File browser
        with wx.FileDialog(self, "Select firmware file", directory, wildcard="BIN files (*.bin;*.ebin)|*.bin;*.ebin",
                      style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fileDialog:
            if fileDialog.ShowModal() == wx.ID_CANCEL:
                # Transmit an NMT reboot command to this node - No need for reboot! 
                # print("Rebooting puck")
                # self.network.send_message(0x0, [0x81, int(self.node_id)])
                # time.sleep(0.5) # wait for puck to reboot (avoids loss of communication)
                # # self.network.send_message(0x4, [self.LAUNCH, int(node_id)])
                # self.configure_Puck()
                if self.adcWasON == True:
                    self.on_off_adc(self)
                return     # the user changed their mind
            # Proceed loading the file chosen by the user
            pathname = fileDialog.GetPath()

        for i in self.network.scanner.nodes:
            print(i)
            indexID = self.network.scanner.nodes.index(i)
            self.choice_id.SetSelection(indexID) # Move to next ID for calibration
            self.select_id(None)

            print("Updating firmware for Puck {}".format(self.getID()))
            self.browse_fw(self,pathname)

        indexID = self.network.scanner.nodes.index(starting_id)
        self.choice_id.SetSelection(indexID) # Return to starting ID after completion
        self.select_id(None)

    def get_version(self, vers): # Convert uint32_t to semantic version: Major.Minor.Patch
        return "{0}.{1}.{2}".format(
            (vers >> 24) & 0xFF, (vers >> 8) & 0xFFFF, (vers & 0xFF))

    def system_config(self, event, filepath=False): 
        if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            return False
        if filepath == False:
          # File browser
          if platform.system() == "Windows":
              directory = '../'
          else:
              directory = ''

          # File browser
          with wx.FileDialog(self, "Select firmware file", directory, wildcard="Configu files (*.ini|*.ini",
                        style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fileDialog:
              if fileDialog.ShowModal() == wx.ID_CANCEL:
                  # Transmit an NMT reboot command to this node
                  # print("Rebooting puck")
                  # self.network.send_message(0x0, [0x81, int(self.node_id)])
                  # time.sleep(0.5) # wait for puck to reboot (avoids loss of communication)
                  # self.network.send_message(0x4, [self.LAUNCH, int(node_id)])
                  # self.configure_Puck()
                  # if self.adcWasON == True:
                  #     self.on_off_adc(self)
                  return     # the user changed their mind
              # Proceed loading the file chosen by the user
              filepath = fileDialog.GetPath()
        else:
            filepath = filepath
    
        print("Reading config file...")
        config = configparser.ConfigParser()
        config.read(filepath)
        options = config.sections()
        for option in options:
            print(option)
            config_id = int(config[option]['ID'])
            fw_version = config[option].get('fw_version')
            fwpath = _strip_ini_quotes(config[option].get('fw'))
            if config_id == self.ID: # Check if config_id is in getNodes()
                print("Found defaults for Puck {}!".format(config_id))
                # firmware
                version = self.get_version(self.node.sdo['MfgSoftwareVersion'].raw)
                # print(version)
                if fw_version and fwpath and version != fw_version:
                  print('Version {} found. Updating firmware to {}'.format(version, fw_version))
                  self.browse_fw(None, fwpath)
                else:
                  print('Version {} found.'.format(version))
                csvpath = _strip_ini_quotes(config[option]['CSV'])
                # print(csvpath)
                self.file_to_p3(None, csvpath)
                break
            else:
                print('Puck {} Not found...'.format(config_id))

        # Should tell user calibration is required, and ask to perform 'calibrate all'
        msg = "Calibration is required after configuration. Would you like to calibrate all Pucks?"
        dlg = wx.MessageDialog(None,msg,'Warning!',wx.YES_NO | wx.ICON_WARNING)
        answer = dlg.ShowModal()
        if answer == wx.ID_YES:
           self.calibrate_all_pucks(None)
        else:
           pass
        dlg.Destroy()

    def check_for_node(self):
      #  print('Checking')
      if len(self.network.scanner.nodes) == 0:
        print('No Active Puck! Ending process...')
        return False
      else:
        return True

    # def upload_system_config(self,event):
    #    print('uploading...')

    # def tune_gains(self, event):  # wxGlade: wxp3_frame.<event_handler>
    #     print("Event handler 'tune_gains' not implemented!")
    #     event.Skip()

    # def save_calibration(self, event):  # wxGlade: wxp3_frame.<event_handler>
    #     print("Event handler 'save_calibration' not implemented!")
    #     event.Skip()
    
    def exit_program(self, event):  # wxGlade: wxp3_frame.<event_handler>
        self.Close()
