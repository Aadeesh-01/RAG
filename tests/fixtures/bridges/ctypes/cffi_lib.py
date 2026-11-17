"""The zoo C library from Python, through cffi (ABI mode)."""

from cffi import FFI

ffi = FFI()
ffi.cdef("int zoo_scale(int value, int factor);")
C = ffi.dlopen("zoo")


def scale(value, factor=2):
    """Multiply in C; the library caps the result at a million."""
    return C.zoo_scale(int(value), int(factor))
