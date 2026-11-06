# SPDX-License-Identifier: GPL-2.0
KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) W=1 KCFLAGS=-Werror modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean

# Deliberately no install or DKMS target, and no DMI autoload alias.
.PHONY: all clean
