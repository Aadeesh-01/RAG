"""The zoo C library from Python, through ctypes."""

import ctypes

_lib = ctypes.CDLL("./build/libzoo.so")
_lib.zoo_add.argtypes = (ctypes.c_int, ctypes.c_int)


def add(a, b):
    """Add two numbers in C, capped at 1000 by the library."""
    return _lib.zoo_add(int(a), int(b))


class Counter:
    """A counter kept by the C library; each bump adds one."""

    def __init__(self):
        self._lib = ctypes.cdll.LoadLibrary("libzoo.so")
        self._value = ctypes.c_int(0)

    def bump(self):
        """Add one in C and return the new value (at most 99)."""
        return self._lib.zoo_bump(ctypes.byref(self._value))


def not_a_library(helper):
    """`helper` is any object: its zoo_unused method is not the C function."""
    return helper.zoo_unused(3) + helper.zoo_add(1, 2)
