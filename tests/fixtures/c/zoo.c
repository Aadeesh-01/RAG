#include <ctype.h>
#include <stdio.h>
#include "zoo.h"

/* An animal with a name and the sound it makes. */
struct Animal {
    const char *name;   /* shown in greetings */
    const char *sound;  /* what it says */
    int legs;
};

/* A dog: an animal (its first member) that can fetch things. */
struct Dog {
    struct Animal base; /* C has no inheritance: a dog starts with an animal */
    int fetches;
};

/* Upper-case a line of text in place and return it. */
char *shout(char *text) {
    for (char *p = text; *p; p++) {
        *p = (char)toupper((unsigned char)*p);
    }
    return text;
}

/* Build the greeting line for an animal into `out`. */
char *format_greeting(const struct Animal *animal, char *out, size_t size) {
    snprintf(out, size, "%s says %s", animal->name, animal->sound);
    return shout(out);
}
