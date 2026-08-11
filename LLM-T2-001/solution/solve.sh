#!/bin/bash
set -euo pipefail

mkdir -p /app/output
cp -R /solution/golden_output/. /app/output/

mkdir -p /app/solution
cp -R /solution/oracle_source/. /app/solution/