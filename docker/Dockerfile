# motiongrametry in one image: the app and web UI, COLMAP (pycolmap), OpenMVS and the mask models'
# runtime. VARIANT=nvidia builds OpenMVS with CUDA and installs the GPU builds of COLMAP and ONNX
# Runtime; every step still runs on the CPU when no GPU is passed in.
#
#   docker build -f docker/Dockerfile -t motiongrametry:nvidia .        (from the project root)
#
# Usually through docker/install.sh, which also sets up the data folder and starts it. AI model
# weights are not in the image: they download on first use into /data/models.
# OpenMVS is built with CUDA 12.9 and carries its CUDA runtime inside; the app's base is CUDA 13.2,
# which ONNX Runtime's GPU build needs. Together they need an NVIDIA driver >= 580.
ARG CUDA_BUILD=12.9.1
ARG CUDA=13.2.0
ARG UBUNTU=24.04

# ---- OpenMVS, built from its unmodified v2.3.0 source (AGPL-3.0; the source ships in the image) ----
FROM nvidia/cuda:${CUDA_BUILD}-devel-ubuntu${UBUNTU} AS openmvs
ARG OPENMVS_TAG=v2.3.0
ARG VCG_COMMIT=658ba36d0a5666650da6e066b4794efc5a463407
# Turing (RTX 20) to Blackwell (RTX 50); each architecture adds build time
ARG CUDA_ARCHS="75;80;86;89;120"
ARG JOBS=8
RUN apt-get update -yq && DEBIAN_FRONTEND=noninteractive apt-get install -yq --no-install-recommends \
        build-essential git cmake ca-certificates \
        libeigen3-dev libcgal-dev libgmp-dev libmpfr-dev libnanoflann-dev \
        libboost-iostreams-dev libboost-program-options-dev libboost-system-dev \
        libboost-serialization-dev libboost-thread-dev \
        libopencv-dev libpng-dev libjpeg-dev libtiff-dev libjxl-dev pkg-config zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
# Ubuntu 24.04's GCC defaults to _FORTIFY_SOURCE=3, which aborts DensifyPointCloud on an
# over-declared snprintf length in SEACAVE::Util::formatTime; upstream builds without it.
RUN git clone https://github.com/cdcseacave/VCG.git /src/vcglib && git -C /src/vcglib checkout -q ${VCG_COMMIT} \
 && git clone --depth 1 --branch ${OPENMVS_TAG} https://github.com/cdcseacave/openMVS.git /src/openMVS \
 && mkdir -p /out/doc \
 && tar czf /out/doc/openmvs-${OPENMVS_TAG}-source.tar.gz --exclude=.git -C /src openMVS \
 && tar czf /out/doc/vcglib-${VCG_COMMIT}-source.tar.gz --exclude=.git -C /src vcglib \
 && cp /src/openMVS/LICENSE /out/doc/OpenMVS-LICENSE-AGPL-3.0.txt \
 && cp /src/vcglib/LICENSE.txt /out/doc/VCGLib-LICENSE-GPL-3.0.txt
# Two builds: CUDA links the driver library (libcuda), which only exists where an NVIDIA driver is
# passed in, so a CPU build runs everywhere else (the launcher below picks one). The CUDA build links
# against the toolkit's stub; the real library comes from the host at run time.
RUN cmake -S /src/openMVS -B /src/build-cuda -DCMAKE_BUILD_TYPE=Release -DVCG_ROOT=/src/vcglib \
        -DOpenMVS_USE_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCHS}" \
        -DCUDA_CUDA_LIBRARY=/usr/local/cuda/lib64/stubs/libcuda.so \
        -DOpenMVS_USE_PYTHON=OFF -DCMAKE_CXX_FLAGS="-U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0" \
 && cmake --build /src/build-cuda -j ${JOBS} \
 && cmake --install /src/build-cuda && mv /usr/local/bin/OpenMVS /out/cuda
RUN cmake -S /src/openMVS -B /src/build-cpu -DCMAKE_BUILD_TYPE=Release -DVCG_ROOT=/src/vcglib \
        -DOpenMVS_USE_CUDA=OFF -DOpenMVS_USE_PYTHON=OFF -DCMAKE_CXX_FLAGS="-U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0" \
 && cmake --build /src/build-cpu -j ${JOBS} \
 && cmake --install /src/build-cpu && mv /usr/local/bin/OpenMVS /out/cpu
COPY docker/openmvs-SOURCE.txt /out/doc/SOURCE.txt

# ---- the app ----
FROM nvidia/cuda:${CUDA}-cudnn-runtime-ubuntu${UBUNTU}
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
RUN apt-get update -yq && DEBIAN_FRONTEND=noninteractive apt-get install -yq --no-install-recommends \
        python3.12 python3.12-venv ca-certificates curl \
        libgl1 libegl1 libusb-1.0-0 libglib2.0-0t64 libgomp1 libx11-6 libxext6 libsm6 libice6 \
        libboost-iostreams1.83.0 libboost-program-options1.83.0 libboost-serialization1.83.0 \
        libboost-system1.83.0 libboost-thread1.83.0 libgmp10 libmpfr6 \
        libopencv-core406t64 libopencv-imgproc406t64 libopencv-imgcodecs406t64 libopencv-calib3d406t64 \
        libopencv-features2d406t64 libopencv-flann406t64 \
        libpng16-16t64 libjpeg8 libtiff6 libjxl0.7 zlib1g \
    && rm -rf /var/lib/apt/lists/*
COPY --from=openmvs /out/cuda /opt/openmvs/cuda
COPY --from=openmvs /out/cpu /opt/openmvs/cpu
COPY --from=openmvs /out/doc /usr/share/doc/openmvs
COPY docker/openmvs.Dockerfile docker/Dockerfile /usr/share/doc/openmvs/build-recipes/
COPY docker/openmvs-launch.sh /opt/openmvs/launch
# Fail the build, not the first run, if a library OpenMVS needs is missing (libcuda: from the host).
RUN mkdir /opt/openmvs/bin && for b in InterfaceCOLMAP DensifyPointCloud ReconstructMesh RefineMesh TextureMesh; do \
        ldd /opt/openmvs/cpu/$b | grep "not found" && exit 1; \
        ldd /opt/openmvs/cuda/$b | grep "not found" | grep -v libcuda.so && exit 1; \
        ln -s ../launch /opt/openmvs/bin/$b; done; chmod +x /opt/openmvs/launch

ENV VIRTUAL_ENV=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY docker/requirements-nvidia.txt /tmp/requirements.txt
RUN uv venv -q --python /usr/bin/python3.12 /opt/venv && uv pip install -q --no-cache -r /tmp/requirements.txt \
 && ls -d /opt/venv/lib/python3.12/site-packages/nvidia/*/lib > /etc/ld.so.conf.d/pip-nvidia.conf && ldconfig
# (pycolmap-cuda12 brings its CUDA 12 runtime as pip packages: their lib/ folders are now on the loader path)
COPY pyproject.toml README.md /src/app/
COPY src /src/app/src
# the app itself; its dependencies are the locked ones above (with the GPU builds)
RUN uv pip install -q --no-cache --no-deps /src/app && rm -rf /src/app \
 && /opt/venv/bin/python -c "import motiongrametry.clean, motiongrametry.meshops, motiongrametry.pipeline, motiongrametry.mask, \
motiongrametry.sfm, motiongrametry.mvs, motiongrametry.frames, motiongrametry.credits, motiongrametry.web.server, rembg; print('app imports ok')"
# The GPU builds must find every library they link (CUDA, cuDNN, cuBLAS from the base image or their
# own CUDA wheels); only the driver (libcuda) comes from the host at run time.
RUN set -e; for so in /opt/venv/lib/python3.12/site-packages/onnxruntime/capi/libonnxruntime_providers_cuda.so \
        /opt/venv/lib/python3.12/site-packages/pycolmap/*.so; do \
      ldd "$so" | grep "not found" | grep -v libcuda.so && { echo "missing libraries for $so"; exit 1; }; done; \
    /opt/venv/bin/python -c "import pycolmap, onnxruntime; print('pycolmap', pycolmap.__version__, 'cuda', getattr(pycolmap, 'has_cuda', None)); print('onnxruntime', onnxruntime.get_available_providers())"

ENV PATH=/opt/venv/bin:/opt/openmvs/bin:$PATH \
    HOME=/data/home U2NET_HOME=/data/models/u2net HF_HOME=/data/models/huggingface \
    MG_IN_CONTAINER=1 MG_OPENMVS_CUDA=1 PYTHONUNBUFFERED=1
LABEL org.opencontainers.image.title="motiongrametry" \
      org.opencontainers.image.description="Video to clean, textured 3D models (nvidia variant)" \
      org.opencontainers.image.licenses="see /about in the app; OpenMVS AGPL-3.0, source in /usr/share/doc/openmvs"
VOLUME /data
EXPOSE 8765
WORKDIR /data
# HOME and the model caches live in /data, owned by whoever runs the container (install.sh: you)
CMD ["sh", "-c", "mkdir -p /data/home /data/models /data/workspaces && exec mg ui --host 0.0.0.0 --port 8765 --root /data/workspaces"]
