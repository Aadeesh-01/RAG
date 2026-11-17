#include <jni.h>
#include <string.h>
#include <time.h>

/* com.example.zoo.NativeMath.add(int, int) */
JNIEXPORT jint JNICALL Java_com_example_zoo_NativeMath_add(JNIEnv *env, jobject thiz, jint a, jint b) {
    jint total = a + b;
    return total;
}

/* com.example.zoo.NativeMath.format_name(String): "_" in a Java name is "_1" in C */
JNIEXPORT jstring JNICALL Java_com_example_zoo_NativeMath_format_1name(JNIEnv *env, jobject thiz, jstring name) {
    const char *raw = (*env)->GetStringUTFChars(env, name, 0);
    jstring out = (*env)->NewStringUTF(env, raw);
    (*env)->ReleaseStringUTFChars(env, name, raw);
    return out;
}

/* com.example.zoo.NativeClock.now_ms(), a static native method */
JNIEXPORT jlong JNICALL Java_com_example_zoo_NativeClock_now_1ms(JNIEnv *env, jclass cls) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (jlong) ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* Not called from this repo: NativeMath.format (no "_" in the Java name, so no "_1"). */
JNIEXPORT jstring JNICALL Java_com_example_zoo_NativeMath_format(JNIEnv *env, jobject thiz, jstring name) {
    const char *raw = (*env)->GetStringUTFChars(env, name, 0);
    return (*env)->NewStringUTF(env, raw);
}
