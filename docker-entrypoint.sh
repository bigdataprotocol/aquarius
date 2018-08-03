#!/bin/bash
export CONFIG_FILE=oceandb.ini
export FLASK_APP=provider_backend/run.py
export FLASK_ENV=development
flask run --host=0.0.0.0
tail -f /dev/null