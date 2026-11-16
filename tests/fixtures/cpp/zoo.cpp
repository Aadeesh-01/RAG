#include <algorithm>
#include <string>

namespace zoo {

std::string shout(const std::string &text);

/// An animal with a name that can speak.
class Animal {
public:
    explicit Animal(std::string name) : name_(std::move(name)) {}
    virtual ~Animal() = default;

    /// What the animal says.
    virtual std::string speak() const {
        return formatGreeting(name_, "makes a sound in the zoo");
    }

protected:
    std::string name_;
};

/// A dog: an animal that barks.
class Dog : public Animal {
public:
    using Animal::Animal;
    std::string speak() const override;
};

/// Build the greeting line for an animal and a sound.
std::string formatGreeting(const std::string &name, const std::string &sound) {
    std::string line = name + " " + sound;
    return shout(line);
}

/// Upper-case a line of text and add an exclamation mark.
std::string shout(const std::string &text) {
    std::string loud = text;
    std::transform(loud.begin(), loud.end(), loud.begin(), ::toupper);
    return loud + "!";
}

}  // namespace zoo

/// Dog::speak is defined outside its class.
std::string zoo::Dog::speak() const {
    std::string base = Animal::speak();
    return zoo::shout(base + " and barks at the mail carrier");
}
