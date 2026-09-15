/* Sample C fixture for repo-map tests. */

#include <stdio.h>

int add(int a, int b) {
    return a + b;
}

void hello(void) {
    printf("Hello, world!\n");
}

int main(int argc, char **argv) {
    hello();
    return add(1, 2);
}
