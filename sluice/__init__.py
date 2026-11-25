"""sluice -- a packet-level TCP laboratory in pure Python.

The package is deliberately layered, and the layers only look downwards:

``seq``
    sequence arithmetic on a circle; no I/O, no state.
``packet``
    the twenty-byte header; bytes in, bytes out.
``link``
    serialisation, propagation and drop-tail queueing.
``cc``
    three congestion controls behind one interface.
``endpoint``
    a sender and a receiver that speak to a three-method environment.
``sim``
    the event loop, the dumbbell, and the ledger that checks every byte.
``experiments``
    named scenarios built from the above, so numbers can be compared.
``render`` / ``cli`` / ``demos``
    presentation, and nothing else.
"""

from .cc import CONTROLS, CongestionControl, Cubic, Reno, Vegas, mathis

__version__ = "1.0.0"

__all__ = [
    "CONTROLS",
    "CongestionControl",
    "Cubic",
    "Reno",
    "Vegas",
    "mathis",
    "__version__",
]
