package com.example.zoo

/** Arithmetic implemented in C, loaded through JNI. */
class NativeMath {
    external fun add(a: Int, b: Int): Int
    external fun format_name(name: String): String

    /** Sum every value with the native add. */
    fun total(values: IntArray): Int {
        var sum = 0
        for (v in values) sum = add(sum, v)
        return sum
    }

    companion object {
        init {
            System.loadLibrary("zoo")
        }
    }
}
