class Animal:
    def __init__(self, name):
        self.name = name

    def speak(self):
        return "..."

    def info(self):
        return f"{self.name} is an animal"


class Mammal(Animal):
    def speak(self):
        return "Some mammal sound"

    def walk(self):
        return f"{self.name} is walking"


class Bird(Animal):
    def speak(self):
        return "Chirp"

    def fly(self):
        return f"{self.name} is flying"


class Bat(Mammal, Bird):
    def speak(self):
        return "Screech"

    def special(self):
        return self.walk() + " and " + self.fly()


class Zoo:
    def __init__(self):
        self.animals = []

    def add_animal(self, animal):
        self.animals.append(animal)

    def show_animals(self):
        for animal in self.animals:
            print(animal.info(), "-", animal.speak())


def create_bat(name="Bruce"):
    bat = Bat(name)
    print(bat.special())
    return bat


def run_zoo():
    zoo = Zoo()
    zoo.add_animal(create_bat())
    zoo.show_animals()


if __name__ == "__main__":
    run_zoo()
