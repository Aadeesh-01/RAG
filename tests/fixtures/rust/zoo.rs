use std::fmt::Write;

/// Anything that can say something, out loud.
pub trait Speak {
    /// What it says.
    fn speak(&self) -> String;
    /// How many times it says it.
    fn repeats(&self) -> usize;
}

/// An animal with a name and the sound it makes.
#[derive(Debug, Clone)]
pub struct Animal {
    pub name: String,
    pub sound: String,
    pub legs: u8,
}

/// A dog: an animal that barks and fetches.
pub struct Dog {
    pub animal: Animal,
    pub fetches: u32,
    pub tricks: Vec<String>,
}

impl Speak for Animal {
    /// What the animal says, in capitals.
    fn speak(&self) -> String {
        format_greeting(&self.name, &format!("{} in the zoo every morning", self.sound))
    }

    fn repeats(&self) -> usize {
        1
    }
}

impl Speak for Dog {
    fn speak(&self) -> String {
        let base = self.animal.speak();
        shout(&format!("{} and barks at the mail carrier", base))
    }

    fn repeats(&self) -> usize {
        self.fetches as usize + 1
    }
}

/// Build the greeting line for an animal and a sound.
pub fn format_greeting(name: &str, sound: &str) -> String {
    let mut line = String::new();
    write!(line, "{} {}", name, sound).unwrap();
    shout(line.trim())
}

/// Upper-case a line of text and add an exclamation mark.
pub fn shout(text: &str) -> String {
    let loud = text.to_uppercase();
    if loud.ends_with('!') { loud } else { format!("{}!", loud) }
}
