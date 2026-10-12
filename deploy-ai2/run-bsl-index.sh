#!/bin/sh
echo "START $(date -u +%Y-%m-%dT%H:%M:%SZ)"
bsl-indexer index --force --path /repos/SRC
echo "EXIT $? $(date -u +%Y-%m-%dT%H:%M:%SZ)"
