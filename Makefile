.PHONY: demo test compile clean

demo:
	python scripts/run_demo.py

test:
	python -m unittest discover -s tests

compile:
	python -m compileall src coverage_lakehouse tests scripts

clean:
	rm -rf data/raw data/staging data/marts
