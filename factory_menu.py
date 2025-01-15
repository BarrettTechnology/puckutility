# factory.py
from subprocess import call
from shutil import copyfile
from os import remove
import wx

class factory():
    def factory_flash(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'factory_flash'")
        # Browse for *.bin
        with wx.FileDialog(self, "Open firmware file", wildcard="BIN files (*.bin)|*.bin",
                        style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fileDialog:

            if fileDialog.ShowModal() == wx.ID_CANCEL:
                return     # the user changed their mind

            self.frame_statusbar.SetStatusText("Flashing puck...", 1)
            self.frame_statusbar.Update()
            wx.Yield()

            # Proceed loading the file chosen by the user
            pathname = fileDialog.GetPath()

            # Copy file to known name (tmp.bin)
            copyfile(pathname, "tmp.bin")

            # Call JLinkExe with jtag_flash.txt
            call(["JLinkExe", "-device", "MK64FX512XXX12", "-commandfile", "jtag_flash.txt"])

            # Delete tmp.bin
            remove("tmp.bin")

            self.frame_statusbar.SetStatusText("Ready", 1)

    def factory_initpuck(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'factory_initpuck' not implemented!")
        event.Skip()

    def factory_testall(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'factory_testall' not implemented!")
        event.Skip()

    def factory_testflash(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'factory_testflash' not implemented!")
        event.Skip()

    def factory_testram(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'factory_testram' not implemented!")
        event.Skip()

    def factory_testee(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'factory_testee' not implemented!")
        event.Skip()

    def factory_testamp(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'factory_testamp' not implemented!")
        event.Skip()

    def factory_testenc(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'factory_testenc' not implemented!")
        event.Skip()