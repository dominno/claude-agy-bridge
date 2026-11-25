"""The slot through which the composition root (agy_bridge.cli.wiring) hands each port its adapter."""
from __future__ import annotations


class Slot:
    """One adapter bound to one port. Use cases call the slot at call time, so a replaced adapter attribute (a test
    double, for example) is what runs."""

    def __init__(self, port):
        self._port = port
        self._adapter = None

    def bind(self, adapter):
        self._adapter = adapter

    def __call__(self):
        if self._adapter is None:
            raise RuntimeError('no adapter is bound to the %s port; agy_bridge.cli wires the adapters' % self._port)
        return self._adapter
