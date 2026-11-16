package zoo

import java.util.Locale

/** An animal with a name that can speak. */
open class Animal(val name: String) {
    /** What the animal says. */
    open fun speak(): String {
        return formatGreeting(name, "makes a sound in the zoo")
    }
}

/** A dog: an animal that barks. */
class Dog(name: String) : Animal(name) {
    override fun speak(): String {
        val base = super.speak()
        return shout(base + " and barks at the mail carrier")
    }
}

/** Build the greeting line for an animal and a sound. */
fun formatGreeting(name: String, sound: String): String {
    val line = "$name $sound"
    return shout(line.trim())
}

/** Upper-case a line of text and add an exclamation mark. */
fun shout(text: String): String {
    val loud = text.uppercase(Locale.ROOT)
    return if (loud.endsWith("!")) loud else "$loud!"
}
