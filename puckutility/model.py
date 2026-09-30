"""model.py — PuckModel, puckutility's model layer.

The bus, node selection and SYNC ownership live in p4core.session.Session;
nothing outside the model starts SYNC.
"""

from p4core.session import Session


class PuckModel(Session):
    """CANopen model for the pucks on one bus (see p4core.session.Session)."""
