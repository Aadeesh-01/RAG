package com.example.zoo;

/** A clock read from native code. */
public class NativeClock {
    static {
        System.loadLibrary("zoo");
    }

    /** Milliseconds since the zoo opened, from C. */
    public static native long now_ms();

    /** Time passed since `start`, in milliseconds. */
    public long elapsed(long start) {
        long now = now_ms();
        return Math.max(0L, now - start);
    }
}
