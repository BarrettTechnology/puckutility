"""calibrate_menu.py — the Menu menu's calibration and puck-management items.

The actuator calibration (cal_actuator.py) and the current-sense bias and
gain items (cal_board.py) are mixins of this class; the frame (app.MyFrame)
inherits it.
"""
import wx
import time
import webbrowser
import configparser
import platform
from p4core.cia402 import SHUTDOWN, MODE_IDLE
from p4core.can_backend import sdo_contention_message
from p4core.calibrate import firmware_version
from p4core import calibrate as calibrate_lib
from p4core import flash as flashp4
from ..paths import _resolve_path, FIRMWARE_DIR, CONFIG_DIR
from p4sysid import drivecal
from .cal_common import _sleep_responsive, GuiCalReporter
from .cal_actuator import ActuatorCal
from .cal_board import BoardCal

# TODO - No active issues


class calibrate(ActuatorCal, BoardCal):
    def _cal_fault(self, exc):
        """Shared cleanup called when an SDO or other exception aborts calibration."""
        print("Calibration fault: {}".format(exc))
        if sdo_contention_message(exc):
            print("  >> Possible CAN bus contention — another app may be "
                  "connected to this bus.")
        msg = drivecal.fault_message(self.node, exc)
        self.OnTaskComplete()
        self.frame_statusbar.SetStatusText("Fault — calibration stopped", 1)
        self._prompt_ok("Calibration Fault", msg)

    def _prompt(self, title, msg):
        """YES/NO dialog — returns True to continue, False to abort.
        Subclasses (or the headless CLI adapter) override this."""
        dlg = wx.MessageDialog(None, msg, title, wx.YES_NO | wx.ICON_WARNING)
        answer = dlg.ShowModal()
        dlg.Destroy()
        return answer == wx.ID_YES

    def _prompt_ok(self, title, msg):
        """Informational OK-only dialog. Subclasses override for headless use."""
        dlg = wx.MessageDialog(None, msg, title, wx.OK | wx.ICON_ERROR)
        dlg.ShowModal()
        dlg.Destroy()

    def _fw_ver_tuple(self):
        return firmware_version(self.node)

    def _fw_at_least(self, major, minor, patch):
        return self._fw_ver_tuple() >= (major, minor, patch)

    def cal_reporter(self):
        """A Reporter for the library calibrations, onto this frame."""
        return GuiCalReporter(self)

    def _menu_idle_takeover(self):
        """Take the drive over cleanly at the start of a MENU-triggered (standalone) drive cal. Resets the
        choice_test selector to Idle (UI only -- the _emcy_selection guard stops SetSelection from re-firing
        select_test's SDOs) AND stops the drive over SDO (zero the command, decelerate, then idle+disable,
        covering any active mode). Otherwise a puck the user left driving via the selector keeps spinning
        into the cal, and the box still reads e.g. 'Vel (RPM)'. Safe no-op if there's no node / SDOs fail.
        Call ONLY on the standalone (calAll == False) path -- in a full cal the parent already took over."""
        try:
            self._emcy_selection = True
            self.choice_test.SetSelection(0)     # 0 = Idle
        except Exception:
            pass
        finally:
            self._emcy_selection = False
        try:
            self.node.sdo['TargetVelocity'].raw = 0
            _sleep_responsive(0.3)               # decelerate a spinning rotor before disabling
            self.node.sdo['SetModeOfOperation'].raw = MODE_IDLE
            self.node.sdo['ControlWord'].raw = SHUTDOWN
        except Exception:
            pass

    def error_compensation_state(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        if self.check_for_node() == False:
            return
        if not self._fw_at_least(4, 4, 0):
            self._prompt_ok("Firmware Too Old",
                "Encoder error compensation requires firmware v4.4.0 or later.\n"
                "Please update the firmware and try again.")
            # Revert the radio selection -- the feature is unavailable on this
            # firmware, so it cannot be turned ON.
            self.frame_menubar.ON.Check(False)
            self.frame_menubar.OFF.Check(True)
            return
        enable = event.GetId() == self.frame_menubar.ON.GetId()
        try:
            drivecal.enc_comp_write(self.node, 1, 1 if enable else 0)
            drivecal.save_single(self.node, 0x3027, 1)
            state_str = "ON" if enable else "OFF"
            print("Encoder error compensation set to {} and saved.".format(state_str))
            self.frame_menubar.ON.Check(enable)
            self.frame_menubar.OFF.Check(not enable)
        except Exception as e:
            print("Error setting encoder compensation state: {}".format(e))

    def set_user_dir(self, event):  # wxGlade: wxp3_frame.<event_handler>
        if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
          return False

        print("Event handler 'set_user_dir'")
        try:
            r = calibrate_lib.set_user_direction(self.node, self.cal_reporter())
        except Exception as exc:
            self._cal_fault(exc)
            return False
        self.frame_statusbar.SetStatusText(
            "User direction set: user polarity {:+d}".format(r['user_polarity']), 1)
        return True

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
                  return     # the user changed their mind
              # Proceed loading the file chosen by the user
              filepath = fileDialog.GetPath()
        else:
            filepath = filepath
    
        print("Reading config file...")
        config = configparser.ConfigParser()
        config.read(filepath)
        options = config.sections()
        _processed = []

        for option in options:
            print(option)
            config_id = int(config[option]['ID'])
            fw_version = config[option].get('fw_version')
            fwpath = _resolve_path(config[option].get('fw'), FIRMWARE_DIR)

            if config_id not in self.network.scanner.nodes:
                print('Puck {} not found on network, skipping'.format(config_id))
                continue

            print("Found defaults for Puck {}!".format(config_id))

            # Switch active node to this puck before any operations
            idx = self.network.scanner.nodes.index(config_id)
            self.choice_id.SetSelection(idx)
            self.select_id(None)

            version = flashp4.read_version(self.node)
            if fw_version and fwpath and version != fw_version:
                print('Version {} found. Updating firmware to {}'.format(version, fw_version))
                self.browse_fw(None, fwpath)
                # browse_fw disconnects and rescans — re-select this puck if still present
                if config_id in self.network.scanner.nodes:
                    idx = self.network.scanner.nodes.index(config_id)
                    self.choice_id.SetSelection(idx)
                    self.select_id(None)
            else:
                print('Version {} found.'.format(version))

            csvpath = _resolve_path(config[option]['CSV'], CONFIG_DIR)
            self.file_to_p4(None, csvpath)
            # file_to_p4 replaces self.network with a fresh canopen.Network() that has
            # no scanner data — rescan so subsequent INI entries can be found.
            self.network.scanner.reset()
            self.network.scanner.search()
            time.sleep(0.5)
            self.choice_id.SetItems([str(i) for i in self.network.scanner.nodes])
            _processed.append(config_id)

        if not _processed:
            print('No matching pucks found in INI file on network.')
            return

        # All pucks updated — offer the actuator calibration once (it asks
        # its own questions: the steps, the motor, saving).
        msg = "Actuator calibration is required after configuration.\nWould you like to calibrate all Pucks?"
        dlg = wx.MessageDialog(None, msg, 'Warning!', wx.YES_NO | wx.ICON_WARNING)
        answer = dlg.ShowModal()
        dlg.Destroy()
        if answer == wx.ID_YES:
            self.calibrate_actuator_all_pucks(None)

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
