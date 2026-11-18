# Real-time IoT analytics — shortcuts
#
#   make setup      install runtime + test dependencies
#   make test       byte-compile sources and run the pytest suite
#   make demo       one-command launcher (simulator + processor + dashboard)
#   make simulator  run the sensor-stream TCP server
#   make processor  run the stream processor (needs the simulator up)
#   make dashboard  run the Streamlit dashboard (needs data in iot.db)
#   make clean      remove runtime data, caches, and pytest artifacts

PY ?= python3
PIP ?= $(PY) -m pip

.PHONY: setup test demo simulator processor dashboard clean

setup:
	$(PIP) install -r requirements.txt

test:
	$(PY) -m compileall -q src config.py run_demo.py
	$(PY) -m pytest -q

demo:
	$(PY) run_demo.py

simulator:
	$(PY) -m src.simulator

processor:
	$(PY) -m src.processor

dashboard:
	$(PY) -m streamlit run src/dashboard.py --server.headless true

clean:
	rm -rf data __pycache__ src/__pycache__ tests/__pycache__ .pytest_cache
	find . -name '*.pyc' -delete
