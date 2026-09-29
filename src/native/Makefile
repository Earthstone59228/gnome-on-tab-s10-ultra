obj-m := defex_off.o

KDIR ?= /kernel

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules CC=clang

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
