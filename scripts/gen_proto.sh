#!/usr/bin/env bash
# Regenerates src/wristconf/_proto/ from proto/garmin/*.proto.
#
# The generated modules are committed, so users never need protoc. Run this only after
# changing a .proto file. The protoc version decides the minimum protobuf runtime
# (protoc 36.x -> protobuf >= 7.36); keep pyproject.toml in sync with it.
set -euo pipefail
cd "$(dirname "$0")/.."
out=src/wristconf/_proto
rm -rf "$out" && mkdir -p "$out"
protoc -Iproto --python_out="$out" proto/garmin/*.proto
# protoc emits absolute imports ("from garmin import x_pb2"); make them package-relative.
sed -i.bak 's/^from garmin import /from wristconf._proto.garmin import /' "$out"/garmin/*_pb2.py
rm -f "$out"/garmin/*.bak
touch "$out/__init__.py" "$out/garmin/__init__.py"
echo "ok: $(ls "$out"/garmin/*_pb2.py | wc -l | tr -d ' ') modules in $out/garmin ($(protoc --version))"
