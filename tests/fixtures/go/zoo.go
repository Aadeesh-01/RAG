package zoo

import (
	"fmt"
	"strings"
)

// Animal is an animal with a name that can speak.
type Animal struct {
	Name  string // shown in greetings
	Sound string // what it says
	Legs  int
}

// Dog is an animal (embedded) that barks and fetches.
type Dog struct {
	Animal
	Fetches int      // how many sticks it brought back
	Tricks  []string // what it can do
}

// Speak says what the animal says, in capitals.
func (a *Animal) Speak() string {
	return FormatGreeting(a.Name, a.Sound+" in the zoo every morning")
}

// Speak makes the dog bark after the usual animal sound.
func (d *Dog) Speak() string {
	base := d.Animal.Speak()
	return Shout(base + " and barks at the mail carrier")
}

// FormatGreeting builds the greeting line for an animal and a sound.
func FormatGreeting(name, sound string) string {
	line := fmt.Sprintf("%s %s", name, sound)
	return Shout(strings.TrimSpace(line))
}

// Shout upper-cases a line of text and adds an exclamation mark.
func Shout(text string) string {
	loud := strings.ToUpper(text)
	if strings.HasSuffix(loud, "!") {
		return loud
	}
	return loud + "!"
}
