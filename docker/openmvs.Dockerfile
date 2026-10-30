# OpenMVS (CPU build) pinned to a release. The image published on Docker Hub
# (openmvs/openmvs-ubuntu:latest) dates from 2019 and lacks `TextureMesh --mesh-file`,
# which the pipeline needs to texture the cleaned mesh.
#
#   docker build -f docker/openmvs.Dockerfile -t motiongrametry/openmvs:2.3.0 docker/
FROM ubuntu:24.04 AS build

# v2.4 needs OpenCV >= 4.11 (JPEG-XL), newer than Ubuntu 24.04 ships; v2.3 has everything we use.
ARG OPENMVS_TAG=v2.3.0
ARG JOBS=8

RUN apt-get update -yq && DEBIAN_FRONTEND=noninteractive apt-get install -yq --no-install-recommends \
        build-essential git cmake ca-certificates \
        libeigen3-dev libcgal-dev libgmp-dev libmpfr-dev libnanoflann-dev \
        libboost-iostreams-dev libboost-program-options-dev libboost-system-dev \
        libboost-serialization-dev libboost-thread-dev \
        libopencv-dev libpng-dev libjpeg-dev libtiff-dev libjxl-dev pkg-config zlib1g-dev \
        libglu1-mesa-dev libglew-dev libglfw3-dev \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu 24.04 GCC defaults to _FORTIFY_SOURCE=3, which aborts DensifyPointCloud on an
# over-declared snprintf length in SEACAVE::Util::formatTime; upstream builds without it.
RUN git clone --depth 1 https://github.com/cdcseacave/VCG.git /src/vcglib \
 && git clone --depth 1 --branch ${OPENMVS_TAG} https://github.com/cdcseacave/openMVS.git /src/openMVS \
 && cmake -S /src/openMVS -B /src/build -DCMAKE_BUILD_TYPE=Release -DVCG_ROOT=/src/vcglib \
        -DOpenMVS_USE_CUDA=OFF -DOpenMVS_USE_PYTHON=OFF -DOpenMVS_BUILD_VIEWER=OFF \
        -DCMAKE_CXX_FLAGS="-U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0" \
 && cmake --build /src/build -j ${JOBS} \
 && cmake --install /src/build

FROM ubuntu:24.04
RUN apt-get update -yq && DEBIAN_FRONTEND=noninteractive apt-get install -yq --no-install-recommends \
        libboost-iostreams1.83.0 libboost-program-options1.83.0 libboost-serialization1.83.0 \
        libboost-system1.83.0 libboost-thread1.83.0 libgmp10 libmpfr6 \
        libopencv-core406t64 libopencv-imgproc406t64 libopencv-imgcodecs406t64 libopencv-calib3d406t64 \
        libopencv-features2d406t64 libopencv-flann406t64 \
        libpng16-16t64 libjpeg8 libtiff6 libjxl0.7 zlib1g libgomp1 \
    && rm -rf /var/lib/apt/lists/*
# OpenMVS installs its apps to /usr/local/bin/OpenMVS whatever the prefix; libs are static.
COPY --from=build /usr/local/bin/OpenMVS /opt/openmvs/bin
ENV PATH=/opt/openmvs/bin:$PATH
WORKDIR /work
# Fail the build (not the first pipeline run) if a runtime library is missing.
RUN for b in InterfaceCOLMAP DensifyPointCloud ReconstructMesh RefineMesh TextureMesh; do \
        ldd /opt/openmvs/bin/$b | grep "not found" && exit 1; done; true
