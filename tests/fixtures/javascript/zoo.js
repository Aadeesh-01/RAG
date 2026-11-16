import { log } from "./util.js";

/** An animal with a name that can speak. */
class Animal {
  constructor(name, legs = 4) {
    this.name = name;
    this.legs = legs;
  }

  // What the animal says, in capitals.
  speak() {
    return formatGreeting(this.name, "makes a sound");
  }
}

/** A dog: an animal that barks and fetches. */
class Dog extends Animal {
  constructor(name, tricks = []) {
    super(name, 4);
    this.tricks = tricks;
  }

  speak() {
    const base = super.speak();
    return shout(base + " and barks at the mail carrier");
  }
}

/** Build the greeting line for an animal and a sound. */
function formatGreeting(name, sound) {
  const line = `${name} ${sound}`;
  return shout(line.trim());
}

// Upper-case a line of text and add an exclamation mark.
export const shout = (text) => {
  const loud = String(text).toUpperCase();
  return loud.endsWith("!") ? loud : loud + "!";
};
