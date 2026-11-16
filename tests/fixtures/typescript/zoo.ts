import { log } from "./util";

/** An animal with a name that can speak. */
export class Animal {
  constructor(public name: string, public legs: number = 4) {}

  /** What the animal says, in capitals. */
  speak(): string {
    return formatGreeting(this.name, "makes a sound in the zoo");
  }
}

/** A dog: an animal that barks and fetches. */
export class Dog extends Animal {
  private tricks: string[] = [];

  speak(): string {
    const base: string = super.speak();
    return shout(base + " and barks at the mail carrier");
  }
}

/** Build the greeting line for an animal and a sound. */
export function formatGreeting(name: string, sound: string): string {
  const line: string = `${name} ${sound}`;
  return shout(line.trim());
}

/** Upper-case a line of text and add an exclamation mark. */
export function shout(text: string): string {
  const loud: string = text.toUpperCase();
  return loud.endsWith("!") ? loud : loud + "!";
}
