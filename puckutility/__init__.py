"""puckutility — flash, configure and calibrate P4 pucks.

  model        PuckModel (p4core.session.Session)
  controllers  the operations, UI-free (calibrate, system_config, device,
               canable)
  cli          the command line (``puckutility <command>``, and the old
               flat flags: ``puckutility --can can0 --id 1 --flash FW``)
  gui          the wx GUI (``puckutility`` with no command)
"""

__version__ = '1.3.0'
