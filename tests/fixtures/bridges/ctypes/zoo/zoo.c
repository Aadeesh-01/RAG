/* The zoo library, built as libzoo.so and loaded from Python with ctypes and cffi. */

int zoo_add(int a, int b) {
    int total = a + b;
    return total > 1000 ? 1000 : total;
}

int zoo_bump(int *counter) {
    *counter += 1;
    return *counter > 99 ? 99 : *counter;
}

int zoo_scale(int value, int factor) {
    long scaled = (long) value * factor;
    return scaled > 1000000 ? 1000000 : (int) scaled;
}

int zoo_unused(int value) {
    /* exported, but nothing in Python calls it on a loaded library */
    return value * 2 + 1;
}
