.PHONY: install uninstall test

install:
	./install.sh

uninstall:
	./uninstall.sh

test:
	python3 test_failpaths.py
	python3 test_render.py
	python3 test_profile.py
