package zoo;

import java.util.Locale;

/** An animal with a name that can speak. */
class Animal {
    protected final String name;

    Animal(String name) {
        this.name = name;
    }

    /** What the animal says. */
    String speak() {
        return Zoo.formatGreeting(name, "makes a sound in the zoo");
    }
}

/** A dog: an animal that barks. */
class Dog extends Animal {
    Dog(String name) {
        super(name);
    }

    @Override
    String speak() {
        String base = super.speak();
        return Zoo.shout(base + " and barks at the mail carrier");
    }
}

/** Helpers that format what animals say. */
public class Zoo {
    /** Build the greeting line for an animal and a sound. */
    static String formatGreeting(String name, String sound) {
        String line = name + " " + sound;
        return shout(line.trim());
    }

    /** Upper-case a line of text and add an exclamation mark. */
    static String shout(String text) {
        String loud = text.toUpperCase(Locale.ROOT);
        return loud.endsWith("!") ? loud : loud + "!";
    }
}
