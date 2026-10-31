#!/usr/bin/env bash
# motiongrammetry: install and start everything (Docker). See docker/install.sh --help for the options.
exec "$(dirname "${BASH_SOURCE[0]}")/docker/install.sh" "$@"
