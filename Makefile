PREFIX ?= /usr/local
BIN    := $(PREFIX)/bin/paneltest
SRC    := src/paneltest/paneltest.py

.PHONY: help test install uninstall clean

help:
	@echo "make test      - chay selftest (khong can phan cung)"
	@echo "make install   - cai vao $(BIN)"
	@echo "make uninstall - go cai dat"

test:
	python3 $(SRC) selftest

install:
	install -D -m 0755 $(SRC) $(BIN)
	@echo "Da cai: $(BIN)   -> thu bang: paneltest selftest"

uninstall:
	rm -f $(BIN)

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
