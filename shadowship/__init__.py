"""ShadowShip: agents that rehearse your release before it ships."""

__version__ = "0.1.0"

import logging

# Library logging stays quiet unless the caller configures logging; the CLI reports status itself.
logging.getLogger("shadowship").addHandler(logging.NullHandler())
