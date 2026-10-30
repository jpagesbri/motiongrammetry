#!/bin/sh
# OpenMVS launcher (installed as each OpenMVS program's name): the CUDA build when the NVIDIA driver
# was passed into the container (--gpus / NVIDIA Container Toolkit), else the CPU build.
app=$(basename "$0")
if [ -e /dev/nvidiactl ] && ldconfig -p 2>/dev/null | grep -q 'libcuda\.so\.1 '; then
    exec /opt/openmvs/cuda/"$app" "$@"
fi
exec /opt/openmvs/cpu/"$app" "$@"
