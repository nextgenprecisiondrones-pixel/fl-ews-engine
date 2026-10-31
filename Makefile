install:
	pip install -e ".[dev]"

data:
	python -m ews.synth --out data --seed 42

test:
	python -m pytest -q

run:
	uvicorn ews.api:app --host 0.0.0.0 --port 8000

ask:
	python -m ews.agent "$(Q)"

.PHONY: install data test run ask
